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

import base64
import os
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import __version__, indexer, migrate as migrate_mod, repo, settings
from . import downloader as downloader_mod
from . import ffmpeg_installer as ffmpeg_mod
from . import google_auth
from . import legal as legal_mod
from . import catalog
from . import platforms as platforms_mod
from . import repack as repack_mod
from . import schedule as schedule_mod
from . import settings_schema, sources
from . import storages as storages_mod
from . import wormhole as wormhole_mod
from . import verify as verify_mod
from .mesh import dht as mesh_dht
from .mesh import channel as mesh_channel
from .mesh import client as mesh_client
from .mesh import feedback as mesh_feedback
from .mesh import fetch as mesh_fetch
from .mesh import firewall as mesh_firewall
from .mesh import identity as mesh_identity
from .mesh import mdns as mesh_mdns
from .mesh import nat as mesh_nat
from .mesh import pacing as mesh_pacing
from .mesh import server as mesh_server
from .db import SYNC_MODES, Database
from .metadata import normalize_video, video_url
from .paths import log_path, profile_dir
from .queue import DownloadWorker
from .util import human_size, now_iso

MAX_LOG = 2000          # строк в кольцевом буфере памяти
MAX_LOG_BYTES = 1_000_000   # ротация файла журнала: больше - обрезаем
KEEP_LOG_BYTES = 512_000    # сколько хвоста храним после ротации
THUMB_MAX_BYTES = 4 * 1024 * 1024   # обложку больше этой не тащим в data-URI
THUMB_BATCH_LIMIT = 100       # id за один вызов get_thumbs
THUMB_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".png": "image/png", ".webp": "image/webp",
              ".avif": "image/avif"}


def _thumb_data_uri(path: str) -> str | None:
    """Локальный файл обложки -> data-URI (None: нет файла/слишком большой).

    Так, а не по URL картинки с площадки: лишний сетевой запрос и след,
    а файл уже лежит рядом с видео вместе с библиотекой.
    """
    try:
        with open(path, "rb") as handle:
            payload = handle.read(THUMB_MAX_BYTES + 1)
    except OSError:
        return None
    if len(payload) > THUMB_MAX_BYTES:
        return None
    mime = THUMB_MIME.get(os.path.splitext(path)[1].lower(),
                          "application/octet-stream")
    return ("data:" + mime + ";base64," +
            base64.b64encode(payload).decode("ascii"))
HEAVY_TTL = 1.0  # секунд между пересчётом агрегатов


def _log_line(text: str) -> str:
    return f"[{time.strftime('%H:%M:%S')}] {text}"


def _sources_with_accounts(conn) -> list[dict]:
    """Источники + подписи их аккаунтов.

    Метки аккаунтов живут в accounts.json (не в БД), поэтому JOIN тут
    невозможен - подмешиваем из реестра, как «куда качать» из хранилищ.
    """
    labels = google_auth.account_labels()
    out = []
    for source in repo.sources(conn):
        item = dict(source)
        account_id = item.get("account_id")
        item["account_label"] = labels.get(account_id, "") if account_id else ""
        out.append(item)
    return out


def _fallback_url(row: dict) -> str:
    """Ссылка на площадку для строки без webpage_url - по её платформе."""
    return (video_url(row.get("platform"), row.get("remote_id"))
            or "")


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
        # Легаси G1: одиночный google_cookies.bin -> первый аккаунт реестра.
        try:
            migrated = google_auth.migrate_legacy(settings.read_raw())
        except Exception:  # noqa: BLE001 - миграция не должна ронять старт
            migrated = None
        if migrated:
            print(f"[аккаунт] старая копия кук перенесена в реестр "
                  f"(id {migrated})")
        # Призраки (запись без копии кук) - в реестр не попадают.
        try:
            pruned = google_auth.prune_registry().get("removed") or []
        except Exception:  # noqa: BLE001 - чистка не должна ронять старт
            pruned = []
        if pruned:
            print(f"[аккаунт] убраны записи без копий кук: "
                  f"{', '.join(pruned)}")
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
        # Переупаковка (переименование по шаблону внутри хранилища).
        self._repack: dict = {"running": False, "done": 0, "total": 0,
                              "current": "", "summary": None, "error": None,
                              "errors": []}
        self._repack_stop = threading.Event()
        self._repack_thread: threading.Thread | None = None
        # Проверка целостности: сверка файлов с хешем в индексе.
        self._verify: dict = {"running": False, "done": 0, "total": 0,
                              "current": "", "checked": 0, "filled": 0,
                              "broken": [], "broken_total": 0,
                              "missing": [], "missing_total": 0,
                              "summary": None, "error": None}
        self._verify_stop = threading.Event()
        self._verify_thread: threading.Thread | None = None
        # Обновление метаданных: подтянуть с площадки лайки/комменты/даты
        # по уже известным видео - без перекачки файлов.
        self._refresh: dict = {"running": False, "done": 0, "total": 0,
                               "current": "", "errors": 0, "summary": None}
        self._refresh_stop = threading.Event()
        self._refresh_thread: threading.Thread | None = None
        # ffmpeg: установка ТОЛЬКО по кнопке пользователя (GPL не вшиваем).
        self._ffmpeg: dict = {"running": False, "phase": "", "pct": 0,
                              "error": None}
        self._ffmpeg_stop = threading.Event()
        self._ffmpeg_thread: threading.Thread | None = None
        # Mesh: сервер участника (поднимается только при включённом
        # участии - настройки mesh_*) и состояние запроса «видео у
        # участников» из карточки.
        config0 = self._current_settings()
        self._mesh_port = int(config0.get("mesh_port") or 38574)
        self._server_limits = (mesh_pacing.mb_to_bytes(config0.get("mesh_max_mb")),
                               mesh_pacing.mbit_to_bps(config0.get("mesh_rate_mbit")))
        self._mesh_server = mesh_server.PeerServer(
            self.db, self._mesh_port, log=self._log,
            max_bytes=self._server_limits[0],
            rate_bps=self._server_limits[1])
        # UDP-узел DHT (тот же номер порта - другой транспорт): вход в
        # сеть через публичные bootstrap-роутеры + LAN-multicast; живые
        # соседи помнятся в профиле между запусками.
        self._mesh_dht = mesh_dht.DHTNode(
            log=self._log, port=self._mesh_port,
            persist=str(profile_dir() / mesh_dht.PEER_FILE))
        # Хранилище доверия (B0): TOFU-пины адресов переносим в список
        # устройств (идемпотентно; решения человека не трогает).
        self._truststore_seed()
        self._mesh: dict = {"running": False, "video_id": None,
                            "title": "", "error": None, "summary": None}
        self._mesh_stop = threading.Event()
        self._mesh_thread: threading.Thread | None = None
        # Каталог площадки (CAT-1..CAT-4): прогон карточек грузит воркер -
        # сетевой запрос не блокирует API окна, ход виден по progress,
        # карточки приходят частями, прогон отменяем. restart складывает
        # новый запрос в pending: перехват стартует, когда старый выйдет.
        self._cat: dict = {"running": False, "platform": "", "mode": "",
                           "query": "", "page": 1, "items": [],
                           "has_more": False, "end": False, "partial": False,
                           "pages": 0, "next_page": 1,
                           "max_pages": catalog.MAX_PAGES,
                           "progress": {"done": 0, "total": 0, "cards": 0},
                           "stage": "", "error": None, "kind": None,
                           "source": "", "done_at": None}
        self._cat_pending: dict | None = None
        self._cat_stop = threading.Event()
        self._cat_thread: threading.Thread | None = None
        self._announce_thread: threading.Thread | None = None
        self._nick_thread: threading.Thread | None = None
        self._nick_dirty = False    # «ник меняли, пока объявляли»
        self._nick_last: dict = {}  # последний результат объявления ника
        self._fw_port_done: int | None = None  # правило брандмауэра: пытался
        self._dpi_hinted = False    # предупреждение про зону перехвата: раз
        # UPnP-проброс (PortMapper): живёт только при включённом участии
        # и согласии на проброс; создаётся в _mesh_apply лениво.
        self._nat: mesh_nat.PortMapper | None = None
        # mDNS (B4): объявление и поиск участников в LAN; библиотека
        # zeroconf опциональна - без неё старт честно отключается строкой
        # в журнале (см. app.mesh.mdns).
        self._mdns_announcer = None
        self._mdns_browser = None
        self._mdns_found: dict = {}   # addr -> {port, nick, ts}
        # Разовая передача по коду (B1): один процесс wormhole за раз.
        self._wormhole_busy = False
        # SAS-сверка в полёте (1.1): слова показаны, ждём подтверждения
        # человека; живёт 10 минут.
        self._sas_pending: dict | None = None
        # Health-check устройств (1.4): фоновый TLS-probe, живёт при
        # включённом участии.
        self._health = None
        # Аккаунт Google: окно входа живёт отдельно от главного.
        self._login_window = None
        self._login_cancel = threading.Event()
        self._login_thread: threading.Thread | None = None
        self._account_note = ""
        self._login_facts: dict = {}   # диагностика поимки (без значений)
        # Качалка: очередь живёт в таблице videos, воркер - фоновый поток.
        self.dl = DownloadWorker(self.db, self._current_settings, log=self._log)
        settings.reload_if_changed(self._settings_cache)
        self._log("Индекс открыт: " + str(self.db.path))
        # Mesh-сервер: поднимется только если участник уже включался.
        self._mesh_apply()
        # Версия сборки: в журнале всегда видно, что за exe работает
        # (значение одно на весь проект - app.__version__).
        self._log(f"Omnistash {__version__}")
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

        # Расписание: таймер живёт вместе с окном; для работы без окна
        # есть omnistash.py --sync под планировщик Windows (см. README).
        self.sched = schedule_mod.Scheduler(self._scheduled_scan,
                                            self._scheduled_sync,
                                            log=self._log)
        self.sched.apply_settings(config)
        self.sched.start()
        self._log(schedule_mod.describe(
            self.sched.state()["scan"], "Автоскан") + " | " +
            schedule_mod.describe(self.sched.state()["sync"], "Автосинк"))
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
                    "sources": _sources_with_accounts(conn),
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
            repack_state = dict(self._repack)
            schedule_state = self.sched.state()
            verify_state = dict(self._verify)
            verify_state["broken"] = list(self._verify.get("broken") or [])[:100]
            verify_state["missing"] = list(self._verify.get("missing") or [])[:100]
            refresh_state = dict(self._refresh)
            mesh_state = dict(self._mesh)
            ffmpeg_state = dict(self._ffmpeg)
        # Найден ли ffmpeg - дешёвые stat-вызовы: спрашиваем при каждом
        # опросе, чтобы правда не отставала от установки/удаления руками.
        ff_path = downloader_mod.find_ffmpeg()
        ffmpeg_state["found"] = bool(ff_path)
        ffmpeg_state["path"] = ff_path
        # Очередь уже качала без ffmpeg? -> в панели очереди появится заметка.
        ffmpeg_state["degraded"] = bool(getattr(self.dl, "ffmpeg_warned", False))
        # Какие кодировщики реально есть в сборке (кэш по mtime в downloader):
        # настройка «Перекодировка» гасит недоступные варианты честно.
        ffmpeg_state["encoders"] = (downloader_mod.available_transcoders(ff_path)
                                    if ff_path else [])
        # Аккаунты Google: реестр меток (accounts.json), никаких секретов.
        account_state = {
            "accounts": google_auth.load_registry(),
            "labels": google_auth.account_labels(),
            "logging_in": False,
            "note": "",
            "visible": None,        # диагностика поимки (счётчики, домены)
            "browser_warning": google_auth.browser_warning(
                self._current_settings()),
        }
        with self._lock:
            account_state["logging_in"] = self._login_window is not None
            account_state["note"] = self._account_note
            if self._login_facts:
                account_state["visible"] = dict(self._login_facts)
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
            "repack": repack_state,
            "schedule": schedule_state,
            "verify": verify_state,
            "refresh": refresh_state,
            "mesh": mesh_state,
            "ffmpeg": ffmpeg_state,
            "account": account_state,
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
                rating_min=request.get("rating_min") or None,
                order=request.get("order") or None,
                tag=request.get("tag") or "",
                platform=request.get("platform") or "",
                exclude_adult=bool(request.get("exclude_adult")),
                offset=int(request.get("offset") or 0),
                limit=min(int(request.get("limit") or 200), 500),
            )
        except Exception as exc:  # noqa: BLE001 - окно должно увидеть ошибку
            self._log(f"Ошибка списка: {exc}")
            return {"total": 0, "rows": [], "error": str(exc)}

    # ------------------------------------------------------------------ #
    #  Коллекции-папки (P4/M17): свои группы из видео разных площадок.
    # ------------------------------------------------------------------ #

    def collections_list(self, request=None) -> dict:
        """Коллекции со счётчиками (дерево и диалоги)."""
        return {"ok": True, "collections": repo.collection_list(self.db.conn)}

    def collection_create(self, request=None) -> dict:
        """Создать папку; имя - из запроса окна."""
        request = request or {}
        try:
            cid = repo.collection_create(self.db.conn,
                                         request.get("name"))
        except ValueError as exc:
            return {"error": str(exc)}
        self._log(f"Коллекция создана: {request.get('name')!r}")
        return {"ok": True, "id": cid}

    def collection_rename(self, request=None) -> dict:
        request = request or {}
        try:
            repo.collection_rename(self.db.conn,
                                   int(request.get("id") or 0),
                                   request.get("name"))
        except ValueError as exc:
            return {"error": str(exc)}
        return {"ok": True}

    def collection_delete(self, request=None) -> dict:
        """Удалить папку: строки видео остаются, уходят только связи."""
        request = request or {}
        cid = int((request or {}).get("id") or 0)
        if not cid:
            return {"error": "Не указана коллекция"}
        row = self.db.conn.execute("SELECT name FROM collections WHERE id=?",
                                   (cid,)).fetchone()
        repo.collection_delete(self.db.conn, cid)
        self._log(f"Коллекция удалена: {(row['name'] if row else cid)!r} "
                  "(строки видео нетронуты)")
        return {"ok": True}

    def collection_edit(self, request=None) -> dict:
        """Добавить/убрать строки: {id, add: [видео], remove: [видео]}."""
        request = request or {}
        cid = int(request.get("id") or 0)
        if not cid:
            return {"error": "Не указана коллекция"}
        add = [int(i) for i in (request.get("add") or [])]
        remove = [int(i) for i in (request.get("remove") or [])]
        added = repo.collection_add(self.db.conn, cid, add) if add else 0
        removed = repo.collection_remove(self.db.conn, cid, remove) \
            if remove else 0
        return {"ok": True, "added": added, "removed": removed}

    # ------------------------------------------------------------------ #
    #  Fallback: подкачка из СЕТИ участников (площадка недоступна).
    # ------------------------------------------------------------------ #

    def mesh_request_video(self, request=None) -> dict:
        """Запросить файл у участников сети (явное действие человека).

        Сценарий: площадка заблокирована/упала, а файл есть у людей -
        DHT-объявления и зеркала каталогов дают адреса держателей.
        Запускается только по кнопке; результат - в журнале.
        """
        request = request or {}
        url = str(request.get("url") or "").strip()
        if not url:
            return {"error": "Нужна ссылка на видео"}
        try:
            info = sources.classify(url)
        except Exception as exc:  # noqa: BLE001 - кривая ссылка не должна валить
            return {"error": f"Ссылка не разобрана: {exc}"}
        remote_id = str(info.get("video_id") or "")
        platform = str(info.get("platform") or "")
        if not remote_id or not platform:
            return {"error": "Из ссылки не видно ID видео - сеть не знает, "
                             "что искать"}
        config = self._current_settings()
        if not config.get("mesh_enabled"):
            return {"error": "Участие в mesh выключено: включите его в "
                             "настройках (секция Mesh), чтобы запрашивать "
                             "файлы у участников"}
        with self._lock:
            if self._mesh.get("running"):
                return {"error": "Запрос к участникам уже идёт"}
            self._mesh = {"running": True, "video_id": None,
                          "title": f"сеть: {remote_id}", "error": None,
                          "summary": None}
        self._mesh_stop.clear()
        self._busy = True
        dest = str(config.get("dest_dir") or profile_dir())

        def work() -> None:
            try:
                result = mesh_fetch.fetch_from_network(
                    self.db.conn, remote_id, platform=platform,
                    dest_dir=dest, log=self._log, stop=self._mesh_stop)
            except Exception as exc:  # noqa: BLE001 - воркер не валит
                self._log(f"Mesh: запрос в сеть упал: {exc}")
                result = {"ok": False, "reason": str(exc)}
            with self._lock:
                self._mesh.update(
                    running=False,
                    summary=("Получено из сети участников"
                             if result.get("ok")
                             else f"Сеть не смогла: {result.get('reason')}"),
                    error=None if result.get("ok")
                    else result.get("reason"))
                self._busy = False

        thread = threading.Thread(target=work, name="omnistash-net-fetch",
                                  daemon=True)
        with self._lock:
            self._mesh_thread = thread
        thread.start()
        self._log(f"Mesh: запрос файла {remote_id} ({platform}) у сети "
                  "участников (площадка недоступна)")
        return {"ok": True, "remote_id": remote_id}

    def platforms_list(self, request=None) -> dict:
        """Платформы для вкладок (P2): реестр + наличие контента.

        Авто-плюс-настройки: вкладка показывается для площадки со
        строками в индексе, скрытые пользователем - в settings
        tabs_hidden (список id через запятую). catalog - есть ли
        браузер карточек площадки (site_catalog); channels - есть ли
        там «новое от каналов» (ленты RSS, только YouTube). Флаг adult
        включает гейт 18+.
        """
        counts = {str(row["platform"]): int(row["n"]) for row in
                  self.db.conn.execute(
                      "SELECT platform, COUNT(*) n FROM videos "
                      "WHERE platform <> 'local' GROUP BY platform")}
        hidden = {item.strip() for item in str(
            self._current_settings().get("tabs_hidden") or "").split(",")
            if item.strip()}
        out = []
        for item in platforms_mod.PLATFORMS:
            pid = item["id"]
            out.append({"id": pid, "label": item["label"],
                        "adult": bool(item.get("adult")),
                        "count": counts.get(pid, 0),
                        "hidden": pid in hidden,
                        "catalog": catalog.catalog_supported(pid),
                        "channels": catalog.channels_supported(pid)})
        return {"ok": True, "platforms": out}

    # ------------------------------------------------------------------ #
    #  Каталог площадок (site-like browsing). Имена «site_*»: mesh-зеркало
    #  индекса уже занимает catalog_* (B3, диалог «Участники»).
    # ------------------------------------------------------------------ #

    def site_catalog(self, request=None) -> dict:
        """Список карточек площадки (тренды/поиск/каналы), без скачивания.

        Прогон всего листа идёт воркером: API окна не стоит, загрузка видна
        по progress (channels - «канал 3 из 10», страницы - анимированная
        полоса), карточки приходят частями, отмена сохраняет собранное.
        Свежий полный кэш отдаётся сразу, без воркера.
        restart=True перехватывает текущую загрузку: смена режима или поиска
        не ждёт старого прогона и не подмешивает его карточки в новый список.
        """
        parsed = self._cat_args(request)
        if parsed.get("error"):
            return parsed
        return self._start_cat_run(parsed)

    def _cat_progress(self, res: dict, cards: int, mode: str,
                      prev_done: int = 0) -> dict:
        """Ход прогона для интерфейса: каналы - итог известен заранее
        (сколько каналов читается), страницы площадки - итог не назван,
        тогда полоса анимированная (total 0) со счётчиком карточек."""
        if mode == "channels":
            done = int(res.get("steps_done") or res.get("pages") or 1)
            total = int(res.get("steps_total") or done)
        else:
            done = prev_done + int(res.get("pages") or 1)
            total = 0
        return {"done": done, "total": total, "cards": cards}

    def _start_cat_run(self, parsed: dict) -> dict:
        """Запустить прогон каталога (или перехватить идущий по restart)."""
        platform = parsed["platform"]
        mode, query, page = parsed["mode"], parsed["query"], parsed["page"]
        force, restart = parsed["force"], parsed.get("restart")
        settings = self._current_settings()
        # «Новое от каналов» - список каналов человека (те, чьи видео
        # сохранялись или добавлены как источник-канал).
        channels = None
        if mode == "channels":
            channels = repo.channels_recent(self.db.conn, platform,
                                            limit=catalog.CHANNELS_MAX)
        cached = None
        if not force:
            cached = catalog.peek_all(platform, mode, query, channels)
            if cached and cached.get("end"):
                cached["items"] = self._mark_in_library(cached.get("items")
                                                        or [], platform)
                with self._lock:
                    self._cat.update(
                        running=False, platform=platform, mode=mode,
                        query=query, page=1, items=cached["items"],
                        has_more=False, end=True, partial=False,
                        pages=cached.get("pages") or 1,
                        next_page=cached.get("next_page") or 1,
                        max_pages=cached.get("max_pages") or catalog.MAX_PAGES,
                        progress=self._cat_progress(cached, len(cached["items"]),
                                                   mode),
                        stage="кэш профиля", error=None, kind=None,
                        source="cache", done_at=now_iso())
                return {"ok": True, "done": True, **cached}
            # cached без end: часть карточек уже в кэше, остальное догружает
            # воркер тем же прогоном (кэш страниц внутри - дешёвый).
        with self._lock:
            if self._cat["running"]:
                if not restart:
                    return {"error": "Каталог уже грузится", "running": True}
                # Перехват: старый прогон отменяется, новый стартует, как
                # только старый выйдет (pending - без блокирующих join в API
                # окна; UI по ключу platform/mode/query отличит свой прогон).
                self._cat_pending = parsed
                self._cat_stop.set()
                self._log(f"Каталог: перехват - {platform} {mode}"
                          + (f" «{query}»" if query else "")
                          + ", текущая загрузка отменяется")
                return {"ok": True, "running": True, "restart": True,
                        "platform": platform, "mode": mode, "query": query,
                        "page": page}
            # Возобновление оборванного листа (page > 1): карточки предыдущего
            # прогона того же запроса остаются в списке, догрузка только
            # добавляет новые video_id (дубли страницы не плодят карточку).
            same_key = (page > 1 and self._cat.get("platform") == platform
                        and self._cat.get("mode") == mode
                        and self._cat.get("query") == query)
            prev: list = list(self._cat.get("items") or []) if same_key else []
            prev_pages = int(self._cat.get("pages") or 0) if same_key else 0
            self._cat = {"running": True, "platform": platform, "mode": mode,
                         "query": query, "page": page, "items": prev,
                         "has_more": False, "end": False, "partial": False,
                         "pages": prev_pages + (cached.get("pages") if cached else 0),
                         "next_page": page,
                         "max_pages": catalog.MAX_PAGES,
                         "progress": {"done": prev_pages +
                                          (cached.get("pages") if cached else 0),
                                      "total": 0, "cards": len(prev)},
                         "stage": "", "error": None, "kind": None,
                         "source": "", "done_at": None}
            if cached:
                self._cat["items"] = self._mark_in_library(
                    list(cached.get("items") or []), platform)
        self._cat_stop.clear()
        prev_ids = {str(item.get("video_id") or "") for item in prev}

        def mine() -> bool:
            """Ход/итог относится к этому прогону: чужой (после перехвата)
            не пишет в текущий список."""
            return (self._cat.get("platform") == platform
                    and self._cat.get("mode") == mode
                    and self._cat.get("query") == query)

        def merge(cards: list) -> list:
            """Собранные карточки прогона поверх уже показанных (resumed)."""
            out = list(prev)
            seen = set(prev_ids)
            for item in cards:
                video_id = str(item.get("video_id") or "")
                if not video_id or video_id in seen:
                    continue
                seen.add(video_id)
                out.append(item)
            return out

        def report(done: int, total: int, cards: list, stage: str) -> None:
            with self._lock:
                if not mine():
                    return
                items = self._mark_in_library(merge(cards), platform)
                self._cat["items"] = items
                # done считает шаги прогона целиком (с уже показанными
                # страницами), иначе полоса после догрузки прыгала бы с нуля.
                self._cat["progress"] = {"done": done + prev_pages,
                                         "total": total, "cards": len(items)}
                self._cat["stage"] = stage

        def work() -> None:
            try:
                res = catalog.fetch_all(platform, mode=mode, query=query,
                                        settings=settings, force=force,
                                        log=self._log, channels=channels,
                                        on_progress=report,
                                        stop=self._cat_stop,
                                        max_pages=catalog.MAX_PAGES,
                                        start_page=page)
                items = self._mark_in_library(merge(res.get("items") or []),
                                              platform)
                payload = {"items": items, "has_more": res.get("has_more"),
                           "end": res.get("end"), "partial": res.get("partial"),
                           "pages": prev_pages + (res.get("pages") or 0),
                           "next_page": res.get("next_page"),
                           "max_pages": res.get("max_pages"),
                           "source": res.get("source"), "error": None,
                           "kind": None, "stage": "готово",
                           "progress": self._cat_progress(res, len(items), mode,
                                                          prev_pages)}
            except catalog.CatalogError as exc:
                kind = getattr(exc, "kind", "empty")
                payload = {"items": [], "has_more": False, "end": True,
                           "partial": False, "pages": 0, "next_page": page,
                           "max_pages": catalog.MAX_PAGES, "source": "",
                           "error": str(exc), "kind": kind, "stage": "",
                           "progress": {"done": 0, "total": 0, "cards": 0}}
            except Exception as exc:  # noqa: BLE001 - воркер не валит окно
                payload = {"items": [], "has_more": False, "end": True,
                           "partial": False, "pages": 0, "next_page": page,
                           "max_pages": catalog.MAX_PAGES, "source": "",
                           "error": str(exc), "kind": "empty", "stage": "",
                           "progress": {"done": 0, "total": 0, "cards": 0}}
            with self._lock:
                cancelled = self._cat_stop.is_set()
                if cancelled:
                    payload = {"items": payload.get("items") or [],
                               "has_more": False, "end": True, "partial": True,
                               "pages": payload.get("pages") or 0,
                               "next_page": payload.get("next_page") or page,
                               "max_pages": catalog.MAX_PAGES,
                               "source": payload.get("source") or "",
                               "error": "Загрузка остановлена", "kind": "cancelled",
                               "stage": "остановлено",
                               "progress": payload.get("progress") or {
                                   "done": 0, "total": 0, "cards": 0}}
                    self._log("Каталог: загрузка отменена")
                if mine():
                    self._cat.update(running=False, done_at=now_iso(), **payload)
                pending = self._cat_pending
                self._cat_pending = None
            if pending:
                # Перехват ждал выхода этого прогона - стартуем новый список.
                self._start_cat_run(pending)

        thread = threading.Thread(target=work, name="omnistash-catalog",
                                  daemon=True)
        with self._lock:
            self._cat_thread = thread
        thread.start()
        self._log(f"Каталог: {platform} {mode}"
                  + (f" «{query}»" if query else "")
                  + (f" с стр. {page}" if page > 1 else ""))
        return {"ok": True, "running": True, "platform": platform,
                "mode": mode, "query": query, "page": page}

    def site_catalog_status(self, request=None) -> dict:
        """Ход прогона: карточки, progress (done/total/cards), stage,
        ключ прогона (platform/mode/query) и ошибка по kind."""
        with self._lock:
            state = dict(self._cat)
            state["items"] = list(state.get("items") or [])
            state["progress"] = dict(state.get("progress") or
                                     {"done": 0, "total": 0, "cards": 0})
        state.setdefault("ok", True)
        return state

    def site_catalog_cancel(self, request=None) -> dict:
        """Отменить загрузку каталога (собранные карточки остаются)."""
        self._cat_stop.set()
        return {"ok": True}

    def _cat_args(self, request) -> dict:
        """Разбор запроса каталога + возрастной гейт (один путь для всех).

        page - страница, с которой начинается прогон (возобновление после
        отмены), restart - перехват идущей загрузки.
        """
        request = request or {}
        try:
            platform = str(request.get("platform") or "").lower()
            mode = str(request.get("mode") or "trending")
            query = str(request.get("query") or "")
            page = max(int(request.get("page") or 1), 1)
            force = bool(request.get("force"))
            restart = bool(request.get("restart"))
        except Exception as exc:  # noqa: BLE001 - кривой запрос = понятный отказ
            return {"error": f"Некорректный запрос: {exc}"}
        if not platform:
            return {"error": "Нужна площадка"}
        # Возраст: взрослая площадка только после явного подтверждения 18+
        # (то же правило, что у add_start) - без подтверждения и запроса нет.
        plat = platforms_mod.by_id(platform) or {}
        if plat.get("adult") and not legal_mod.state().get("adult_ok"):
            return {"error": "Нужно подтверждение 18+ (возрастной гейт)",
                    "need_adult": True}
        # «Новое от каналов» - RSS есть только у YouTube: на остальных
        # площадках режим честно сворачивается в тренды (каналов там нет).
        if mode == "channels" and not catalog.channels_supported(platform):
            mode = "trending"
        return {"platform": platform, "mode": mode, "query": query,
                "page": page, "force": force, "restart": restart}

    def site_catalog_more(self, request=None) -> dict:
        """Следующая страница каталога (infinite scroll)."""
        request = dict(request or {})
        request["page"] = max(int(request.get("page") or 1), 1)
        return self.site_catalog(request)

    def site_catalog_channels(self, request=None) -> dict:
        """Каналы человека для чипсов каталога (P3): кто добавлен/сохранял.

        Список по последнему синку: первые - самые «свежие» каналы; чипс
        даёт режим «channel» (страница загрузок канала) и RSS-режим
        «channels» (новое от всех сразу, с датами).
        """
        request = request or {}
        platform = str(request.get("platform") or "").lower() or None
        try:
            limit = min(int(request.get("limit") or catalog.CHANNELS_MAX), 40)
        except Exception:  # noqa: BLE001
            limit = catalog.CHANNELS_MAX
        rows = repo.channels_recent(self.db.conn, platform, limit=limit)
        return {"ok": True, "channels": rows}

    def site_catalog_clear(self, request=None) -> dict:
        """Сброс кэша карточек (одной площадки или всех)."""
        request = request or {}
        platform = str(request.get("platform") or "").lower() or None
        with self._lock:
            self._cat.update(items=[], has_more=False, page=1, pages=0,
                             next_page=1, end=False, partial=False,
                             progress={"done": 0, "total": 0, "cards": 0},
                             stage="")
        return {"ok": True, "removed": catalog.clear_cache(platform)}

    def _mark_in_library(self, items: list, platform: str) -> list:
        """Пометка «уже в библиотеке» по ключу строки (platform:remote_id)."""
        keys = [f"{str(it.get('platform') or platform)}:"
                f"{str(it.get('video_id') or '')}"
                for it in items if it.get("video_id")]
        if not keys:
            return items
        try:
            marks = ",".join("?" for _ in keys)
            present = {str(row["key"]) for row in
                       self.db.conn.execute(
                           f"SELECT key FROM videos WHERE key IN ({marks})",
                           keys)}
        except Exception:  # noqa: BLE001 - пометка не должна портить список
            return items
        for item in items:
            key = (f"{str(item.get('platform') or platform)}:"
                   f"{str(item.get('video_id') or '')}")
            if key in present:
                item["added"] = True
        return items

    def tags_popular(self, request=None) -> dict:
        """Теги площадки счётчиками - чипсы фильтра (PH3, вкладка 18+)."""
        request = request or {}
        try:
            limit = min(int(request.get("limit") or 40), 200)
            platform = str(request.get("platform") or "").strip()
            tags = repo.tags_popular(self.db.conn, platform or None,
                                     limit=limit)
        except Exception as exc:  # noqa: BLE001 - окно должно увидеть ошибку
            return {"ok": False, "error": str(exc), "tags": []}
        return {"ok": True, "tags": tags}

    def get_video(self, video_id) -> dict | None:
        """Карточка видео: колонки + файлы + плейлисты + raw_json."""
        try:
            return repo.video_detail(self.db.conn, int(video_id))
        except (TypeError, ValueError):
            return None

    def _mesh_apply(self) -> None:
        """Поднять/остановить mesh-сервер, DHT-узел И UPnP-проброс.

        Сервер отдаёт только проверенные файлы (с известным sha256) и
        поднимается исключительно при включённом участии (согласие).
        DHT (UDP, тот же порт - другой транспорт) объявляет наши файлы
        и ищет участников без своих серверов. Проброс порта за NAT -
        отдельное согласие (mesh_upnp): роутер или не найден, или отказал
        - остаёмся на LAN, причина пишется в журнал (никаких падений).
        """
        config = self._current_settings()
        want = bool(config.get("mesh_enabled"))
        port = int(config.get("mesh_port") or 38574)
        limits = (mesh_pacing.mb_to_bytes(config.get("mesh_max_mb")),
                  mesh_pacing.mbit_to_bps(config.get("mesh_rate_mbit")))
        if port != self._mesh_port:
            if self._mesh_server.running:
                self._mesh_server.stop()
            if self._mesh_dht.running:
                self._mesh_dht.stop_sync()
            self._nat_stop()    # проброс старого порта нам больше не нужен
            self._mesh_server = mesh_server.PeerServer(
                self.db, port, log=self._log,
                max_bytes=limits[0], rate_bps=limits[1])
            self._server_limits = limits
            self._mesh_dht = mesh_dht.DHTNode(
                log=self._log, port=port,
                persist=str(profile_dir() / mesh_dht.PEER_FILE))
            mesh_dht.set_current(None)
            self._mesh_port = port
        elif self._server_limits != limits:
            # Изменились лимиты переноса - сервер пересоздаём (они живут
            # в обработчике соединений), DHT не трогаем.
            if self._mesh_server.running:
                self._mesh_server.stop()
                self._log("Mesh-сервер: перезапуск - изменился лимит "
                          "переноса")
            self._mesh_server = mesh_server.PeerServer(
                self.db, self._mesh_port, log=self._log,
                max_bytes=limits[0], rate_bps=limits[1])
            self._server_limits = limits
        if want and bool(config.get("mesh_upnp", True)):
            if self._nat is None or self._nat.port != port:
                self._nat_stop()
                mapper = mesh_nat.PortMapper(port, log=self._log)
                self._nat = mapper
                mapper.start()   # фон: SSDP+SOAP к роутеру не блокируют
                self._log(f"UPnP: проброс порта {port} запрошен у роутера "
                          "(TCP+UDP); итог - следующей строкой журнала")
        else:
            self._nat_stop()
        if want:
            # Входящее правило брандмауэра (согласие дано при включении
            # mesh): без него сосед не откроет канал - фоном, один раз
            # за порт, отказ = готовая команда в журнале.
            if self._fw_port_done != port:
                self._fw_port_done = port
                threading.Thread(target=self._firewall_ensure,
                                 args=(port,), daemon=True,
                                 name="omnistash-firewall").start()
            # F2: порт в зоне типового DPI-перехвата + обнаружен
            # перехватчик - честное предупреждение (НЕ выключать его, а
            # сменить порт/добавить исключение).
            if mesh_firewall.in_dpi_zone(port) and not self._dpi_hinted:
                self._dpi_hinted = True
                threading.Thread(target=self._dpi_warn, args=(port,),
                                 daemon=True,
                                 name="omnistash-dpi-hint").start()
            if not self._mesh_server.running:
                try:
                    opened = self._mesh_server.start()
                    self._log(f"Mesh-сервер поднят: порт {opened} "
                              "(TLS, отдаёт только проверенные файлы)")
                except Exception as exc:  # noqa: BLE001 - старта нет = LAN не тронут
                    self._log(f"Mesh-сервер не поднялся (порт {port}): {exc}")
            if not self._mesh_dht.running:
                try:
                    udp = self._mesh_dht.start_sync()
                    mesh_dht.set_current(self._mesh_dht)
                    self._log(f"DHT-узел поднят: udp {udp} "
                              "(вход через публичные роутеры + LAN)")
                    self._start_announce()
                    self._start_nick_announce()
                except RuntimeError as exc:
                    self._log(f"DHT-узел не поднялся: {exc}")
            self._mdns_apply(config)
            self._health_apply(config)
        else:
            if self._mesh_server.running:
                self._mesh_server.stop()
                self._log("Mesh-сервер остановлен")
            if self._mesh_dht.running:
                self._mesh_dht.stop_sync()
                mesh_dht.set_current(None)
                self._log("DHT-узел остановлен")
            self._mdns_stop_all()
            if self._health is not None:
                self._health.stop()

    def _nat_stop(self) -> None:
        """Снять UPnP-проброс (снятие делает фон маппера, тут - только воля)."""
        mapper, self._nat = self._nat, None
        if mapper is not None:
            mapper.stop()

    def _firewall_ensure(self, port: int) -> None:
        """Правило входящего (фоном; сбой - строка в журнале, не авария)."""
        try:
            mesh_firewall.ensure_rules(port, self._log)
        except Exception:  # noqa: BLE001 - брандмауэр не должен валить
            self._log("Брандмауэр: проверка правил не удалась (см. "
                      "инструкцию в журнале выше, если она была)")

    def _dpi_warn(self, port: int) -> None:
        """Порт в зоне перехвата DPI-утилит? Сказать честно, не мешая.

        Пользователи в России живут с zapret-подобными программами -
        мы НЕ просим их выключать: сообщаем, что порт вне зоны
        перехвата (1000) или исключение в их программе решает вопрос.
        """
        try:
            if mesh_firewall.dpi_running():
                self._log(
                    f"Совместимость: обнаружен DPI-перехватчик "
                    f"(zapret/winws), а ваш порт {port} - в зоне их "
                    "перехвата (1024-65535/80/443). Выключать программу "
                    f"НЕ нужно: либо смените порт на 1000 (вне зоны, "
                    "настройка «Порт mesh-сервера»), либо добавьте наш "
                    "порт в исключения её конфига.")
        except Exception:  # noqa: BLE001 - подсказка не должна валить
            pass

    # ------------------------------------------------------------------ #
    #  B4: mDNS-объявление и поиск участников в LAN
    # ------------------------------------------------------------------ #

    def _mdns_apply(self, config: dict) -> None:
        """Поднять/перенастроить mDNS при включённом участии.

        Смена порта = новая пара объявл/поиск; смена ника - мягкая
        перерегистрация сервиса. Без zeroconf оба старта честно
        откажутся строкой в журнале (см. app.mesh.mdns).
        """
        if not config.get("mesh_mdns", True):
            self._mdns_stop_all()
            return
        port = int(config.get("mesh_port") or 38574)
        nick = mesh_dht.clean_nick(config.get("mesh_nick"))
        announcer = self._mdns_announcer
        if announcer is not None and announcer.port == port:
            announcer.set_nick(nick)
            return
        self._mdns_stop_all()
        self._mdns_announcer = mesh_mdns.Announcer(port, nick, log=self._log)
        self._mdns_announcer.start()
        self._mdns_browser = mesh_mdns.Browser(self._mdns_on_found,
                                               log=self._log)
        self._mdns_browser.start()

    def _mdns_stop_all(self) -> None:
        browser, announcer = self._mdns_browser, self._mdns_announcer
        self._mdns_browser = self._mdns_announcer = None
        if browser is not None:
            browser.stop()
        if announcer is not None:
            announcer.stop()

    def _mdns_on_found(self, addr: str, port: int, nick: str) -> None:
        """Колбэк mDNS (свой поток): запомнить найти и оповестить о новых."""
        if port == self._mesh_port and addr == mesh_mdns.lan_ip():
            return                            # это собственное объявление
        with self._lock:
            known = self._mdns_found.get(addr)
            self._mdns_found[addr] = {"port": int(port), "nick": nick,
                                      "ts": time.time()}
        if not known:
            self._log(f"mDNS: найден участник {addr}:{port}"
                      + (f" (ник {nick})" if nick else ""))

    # ------------------------------------------------------------------ #
    #  1.4: health-check устройств (тихий TLS-probe, 5 мин ± джиттер)
    # ------------------------------------------------------------------ #

    def _health_apply(self, config: dict) -> None:
        """Поднять health-check при включённом участии (идемпотентно)."""
        if self._health is None:
            from .mesh import health as mesh_health
            self._health = mesh_health.HealthChecker(
                probe_fn=self._health_probe,
                devices_fn=self._health_devices,
                on_change=self._health_record,
                log=self._log)
        if not self._health.running:
            self._health.start()

    @staticmethod
    def _health_probe(addr: str, expect: str | None = None):
        """Тихая проба: handshake + аттестация, без id и запросов."""
        return mesh_client.probe(addr, expect=expect, pin=False,
                                 timeout=4.0)

    def _health_devices(self) -> list:
        """Кого проверяем: устройства с адресом, без отозванных."""
        out = []
        for device in repo.truststore_list(self.db.conn):
            if device.get("revoked_at") or not device.get("last_addr"):
                continue
            out.append((device["pubkey"], device["last_addr"]))
        return out

    def _health_record(self, pubkey: str, status: str,
                       detail: str = "") -> str | None:
        """Записать статус; при «ожил» обновить свежесть адреса."""
        previous = repo.peer_health_set(self.db.conn, pubkey, status,
                                        detail)
        if status == "alive":
            device = repo.truststore_get(self.db.conn, pubkey)
            addr = str((device or {}).get("last_addr") or "")
            if addr:
                repo.peer_addr_add(self.db.conn, pubkey, addr, "health")
        return previous.get("was")

    def _dht_prepare_announce(self) -> None:
        """Перед анонсом: внешний IP для BEP42 И решение о публикации.

        UPnP уже знает внешний адрес - подсказываем сразу; остальное
        узел выучит из поля ip в ответах соседей (для BEP42-ид). Но
        ПУБЛИКОВАТЬ адрес посторонним можно только при живом пробросе:
        без него публичный адрес мёртв, а у машин за одним NAT он
        одинаков (пара vilmpc/vilmnote = обе 2.60.152.240:38574) -
        чужой поиск пошёл бы на чужой порт.
        """
        mapper = self._nat
        if mapper is not None:
            if mapper.external_ip:
                self._mesh_dht.learn_external_ip(mapper.external_ip)
            state = mapper.state()
            self._mesh_dht.set_publish_public(
                bool(state.get("enabled") and state.get("external_ip")))
        else:
            self._mesh_dht.set_publish_public(False)
        self._mesh_dht.ensure_ready(timeout=15.0)

    def _start_nick_announce(self) -> None:
        """Объявить ник в каталоге имён DHT (фоном, только при участии).

        Смена ника во время объявления НЕ теряется: живой поток молча
        отбрасывал новое значение (в профиле один ник, в DHT другой -
        проверено на живой сессии). Теперь воркер после каждого
        объявления перечитывает настройку и повторяет, пока ник не
        стабилизируется.
        """
        if self._mesh_dht is None or not self._mesh_dht.running:
            return
        self._nick_dirty = True
        if self._nick_thread is not None and self._nick_thread.is_alive():
            return              # работающий воркер сам перечитает (dirty)

        def worker() -> None:
            announced = None
            for _ in range(6):
                self._nick_dirty = False
                config = self._current_settings()
                nick = mesh_dht.clean_nick(config.get("mesh_nick"))
                if not mesh_dht.nick_ok(nick):
                    self._log(f"DHT: ник «{nick}» не объявляется: не "
                              "проходит правила (2-32 символа; буквы, цифры, "
                              "пробел, дефис, подчёркивание)")
                    return
                if nick != announced:
                    if not self._mesh_dht.running:
                        return
                    self._dht_prepare_announce()
                    if not self._mesh_dht.running:
                        return
                    accepted = self._mesh_dht.announce_name_sync(
                        nick, self._mesh_port)
                    announced = nick
                    self._nick_last = {"nick": nick,
                                       "accepted": int(accepted or 0)}
                    if accepted:
                        self._log(f"DHT: ник «{nick}» объявлен (порт "
                                  f"{self._mesh_port}, приняли {accepted})")
                    elif not self._mesh_dht.publish_public:
                        self._log(f"DHT: ник «{nick}» объявлен только "
                                  "локально (UPnP не поднялся - публичный "
                                  "адрес не разбрасываем, LAN-соседи видят "
                                  "напрямую)")
                    else:
                        self._log(f"DHT: ник «{nick}» пока не объявился - "
                                  "повторит maintenance, когда сеть будет "
                                  "доступна")
                if not self._nick_dirty:
                    return          # ник стабилен - всё объявлено
            self._log("DHT: ник меняется слишком часто - сохраните его "
                      "ещё раз, когда поиск успокоится")

        self._nick_thread = threading.Thread(
            target=worker, daemon=True, name="omnistash-mesh-nick")
        self._nick_thread.start()

    def _start_announce(self) -> None:
        """Объявить имеющиеся файлы в DHT - фоном, один раз за включение."""
        if (self._announce_thread is not None
                and self._announce_thread.is_alive()):
            return

        def worker() -> None:
            # Страховка: ручные адреса (mesh_peers) - в таблицу контактов.
            # Если multicast/DHT не помогает (заблокированный hello и
            # т.п.), прямые запросы к известным адресам всё равно работают.
            entries = [part.split("@", 1)[0].strip()
                       for part in
                       str(self._current_settings().get("mesh_peers") or "")
                       .replace(";", ",").split(",") if part.strip()]
            if entries:
                try:
                    added = self._mesh_dht.add_contacts_sync(entries)
                    self._log(f"DHT: ручные адреса в таблице: "
                              f"{added} из {len(entries)}")
                except Exception:  # noqa: BLE001 - страховка не должна валить
                    pass
            conn = self.db.conn
            rows = conn.execute(
                """SELECT DISTINCT v.remote_id
                     FROM videos v JOIN files f ON f.video_id = v.id
                    WHERE f.kind='video' AND f.missing=0
                      AND f.hash IS NOT NULL AND v.platform <> 'local'
                    ORDER BY v.remote_id""").fetchall()
            self._dht_prepare_announce()
            done = 0
            for row in rows:
                if not self._mesh_dht.running:
                    break
                try:
                    self._mesh_dht.announce_sync(row["remote_id"],
                                                 self._mesh_port)
                except Exception:  # noqa: BLE001 - объявление не должно валить
                    pass
                done += 1
                if done % 50 == 0:
                    self._log(f"DHT: объявлено {done}/{len(rows)}")
            if done:
                self._log(f"DHT: объявлено файлов {done} из {len(rows)} "
                          f"(порт {self._mesh_port})")

        self._announce_thread = threading.Thread(
            target=worker, daemon=True, name="omnistash-mesh-announce")
        self._announce_thread.start()

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
        if key in ("mesh_enabled", "mesh_port", "mesh_upnp",
                    "mesh_max_mb", "mesh_rate_mbit"):
            # Согласие/порт/лимиты - сервер и проброс следуют сразу.
            self._mesh_apply()
        if key == "mesh_nick":
            # Смена ника объявляется сразу (при включённом участии):
            # другие участники увидят новый адрес без перезапуска.
            self._start_nick_announce()
        if key in ("scan_interval_min", "sync_interval_min"):
            # Смена интервала переносит срок сразу: не ждём старого.
            self.sched.apply_settings(
                settings.as_public(self._current_settings()))
            self._log("Расписание: " + " | ".join(
                schedule_mod.describe(self.sched.state()[name], label)
                for name, label in (("scan", "автоскан"),
                                    ("sync", "автосинк"))))
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
        current = self._current_settings()
        keep_sidecar = bool(current.get("keep_sidecar", True))
        sidecar_mode = str(current.get("sidecar_mode") or "files")
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
                                  keep_sidecar=keep_sidecar,
                                  sidecar_mode=sidecar_mode)
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
        """Фаза A: снять снапшот с площадки и посчитать план (БД не трогаем).

        auto=True - карточка каталога: одиночное видео добавляется без
        диалога «Добавить источник» (план из одной записи ничего не решает),
        поэтому фаза confirm не показывается и запись идёт сразу. Для
        плейлиста/канала auto игнорируется: там план надо показать.
        Одиночное видео в режиме «Частичная» трактуется как «Полная» -
        пикер на один элемент это просто лишний экран.
        """
        request = request or {}
        url = str(request.get("url") or "").strip()
        if not url:
            return {"error": "Вставьте ссылку на плейлист, канал или видео"}
        auto = bool(request.get("auto"))
        config = self._current_settings()
        mode = request.get("mode") or config.get("default_sync_mode") or "partial"
        if mode not in SYNC_MODES:
            mode = "partial"
        # Куда качать этот источник: выбор пользователя, а не фоновое
        # решение - валидируем сразу, чтобы не узнать об ошибке при загрузке.
        storage_id = str(request.get("storage_id") or "").strip() or None
        if storage_id:
            _storage, error = self._storage_or_error(storage_id)
            if error:
                return {"error": error}
        # Чьи куки использовать: аккаунт привязывается к источнику.
        account_id = str(request.get("account_id") or "").strip() or None
        if account_id and not any(item["id"] == account_id
                                  for item in google_auth.load_registry()):
            return {"error": "Аккаунт не найден - войдите заново"}
        try:
            info = sources.classify(url)
        except Exception as exc:  # noqa: BLE001 - кривая ссылка не должна ронять окно
            return {"error": f"Ссылка не разобрана: {exc}"}
        kind = info.get("kind")
        # Возрастная площадка (PH5/P1): первый URL ВЗРОСЛОЙ площадки по
        # флагу реестра требует явного «мне18+» - окно вернёт
        # adult_confirm, UI спросит и повторит add_start.
        if (platforms_mod.is_adult(info.get("platform"))
                and not legal_mod.state().get("adult_ok")):
            return {"adult_confirm": True,
                    "error": "Возрастной контент: подтвердите, что вам "
                             "есть 18 лет и вы несёте ответственность за "
                             "скачиваемое"}
        if kind not in ("playlist", "channel", "video"):
            return {"error": "Поддерживаются ссылки на плейлист, канал "
                             "и одиночное видео", "kind": "unsupported"}
        if auto and kind == "video" and mode == "partial":
            # «Частичная» для одного видео = пикер из одного элемента:
            # вместо него видео встаёт в очередь («Полная»), а «Ручная»
            # остаётся «только метаданные».
            mode = "full"

        with self._lock:
            if self._add.get("phase") in ("fetching", "committing"):
                return {"error": "Добавление уже идёт"}
            self._add = {"phase": "fetching", "mode": mode,
                         "storage_id": storage_id, "account_id": account_id,
                         "url": url, "auto": auto,
                         "fetch": {"got": 0, "total": 0}, "plan": None,
                         "stages": self._stages(), "result": None, "error": None,
                         "error_kind": None}
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
            if "storage_id" in request:
                # Выбор «куда качать» можно поменять прямо в диалоге
                # подтверждения - фиксируем его к моменту записи.
                storage_id = str(request.get("storage_id") or "").strip() or None
                if storage_id:
                    _storage, error = self._storage_or_error(storage_id)
                    if error:
                        return {"error": error}
                self._add["storage_id"] = storage_id
            if "account_id" in request:
                account_id = str(request.get("account_id") or "").strip() or None
                if account_id and not any(item["id"] == account_id
                                          for item in google_auth.load_registry()):
                    return {"error": "Аккаунт не найден - войдите заново"}
                self._add["account_id"] = account_id
        return self._start_commit()

    def _start_commit(self) -> dict:
        """Запустить стадию записи (фаза C).

        Общий вход для подтверждения плана в диалоге и для карточки
        каталога (auto): одиночное видео пишется тем же конвейером, просто
        без экрана «Проверьте план».
        """
        with self._lock:
            self._add["phase"] = "committing"
            self._add["stages"] = self._stages()
            self._add["error"] = None
            self._add["error_kind"] = None
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
            with self._lock:
                account_id = self._add.get("account_id")
            snapshot = sources.fetch_snapshot(url, settings=config,
                                              on_progress=progress,
                                              stop=self._add_stop,
                                              account_id=account_id)
        except sources.Aborted:
            self._finish_fetch(None, None, error=None, aborted=True)
            return
        except sources.FetchError as exc:
            self._finish_fetch(None, None, error=str(exc),
                               kind=getattr(exc, "kind", "network"))
            return
        except Exception as exc:  # noqa: BLE001 - сбой площадки не должен молчать
            self._log("Индексация упала: " + str(exc))
            self._finish_fetch(None, None,
                               error=f"Не удалось получить источник: {exc}",
                               kind="network")
            return

        plan = repo.plan_diff(self.db.conn, snapshot)
        self._finish_fetch(snapshot, plan)

    def _finish_fetch(self, snapshot, plan, *, error=None, aborted=False,
                      kind=None) -> None:
        # Пользователь мог нажать «Отмена», пока запрос ещё шёл: маленький
        # плейлист успевает прийти целиком, но снимать план в этом случае
        # уже поздно - он просил остановиться.
        if aborted or self._add_stop.is_set():
            aborted, error, snapshot, plan = True, None, None, None
        auto = bool(self._add.get("auto"))
        counts = plan["counts"] if plan else None
        # auto + одиночное видео = карточка каталога: экран плана не
        # показывается, запись идёт сразу (пикер из одной записи ничего не
        # выбирает). Плейлист/канал под auto остаётся с подтверждением.
        direct = (snapshot is not None and auto
                  and str((snapshot.get("playlist") or {}).get("kind") or "")
                  == "video")
        already = (direct and plan["playlist"]["exists"]
                   and counts["new_videos"] == 0)
        commit_direct = direct and not already
        with self._lock:
            if aborted:
                self._add.update(phase="idle", fetch=None, plan=None,
                                 error=None, error_kind=None)
                self._status = "Готов."
            elif error:
                # kind - причина отказа (unavailable/blocked/bot/network):
                # UI по ней предлагает обход/VPN, запрос у участников сети
                # или вход в аккаунт, а не единый «попробовать снова».
                self._add.update(phase="error", fetch=None, plan=None,
                                 error=error, error_kind=kind or "network")
                self._status = "Ошибка"
            elif already:
                # Видео уже в библиотеке: писать нечего - карточка гасится,
                # повторный запрос площадки на то же видео не плодится.
                self._add.update(
                    phase="done", fetch=None, plan=None, error=None,
                    error_kind=None,
                    result={"already": True, "url": snapshot["url"],
                            "title": snapshot["playlist"].get("title"),
                            "mode": self._add.get("mode"), "queued": 0,
                            "stats": {"new_videos": 0,
                                      "known_videos": counts["known_videos"],
                                      "links_to_create": 0}})
                self._busy = False
                self._status = "Уже в библиотеке"
            elif commit_direct:
                self._add.update(phase="committing", fetch=None, plan=None,
                                 error=None, error_kind=None,
                                 stages=self._stages())
                self._add_cancel.clear()
                self._busy = True
                self._status = "Создание записей…"
            else:
                self._add.update(
                    phase="confirm", fetch=None, error=None,
                    error_kind=None,
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
            if not commit_direct:
                self._busy = False
        if commit_direct:
            self._add_snapshot = snapshot
            self._add_plan = plan
            self._log("План: новых {} · уже есть {} · пропущено {}".format(
                counts["new_videos"], counts["known_videos"], counts["skipped"]))
            thread = threading.Thread(target=self._add_commit_worker,
                                      daemon=True)
            self._add_thread = thread
            thread.start()
            return
        if error:
            self._log("Индексация: " + error)
        elif aborted:
            return
        elif already:
            self._log("Уже в библиотеке: " + str(snapshot["url"]))
        else:
            counts = plan["counts"]
            self._log("План: новых {} · уже есть {} · пропущено {}".format(
                counts["new_videos"], counts["known_videos"], counts["skipped"]))

    def _add_commit_worker(self) -> None:
        snapshot = self._add_snapshot
        plan = self._add_plan
        with self._lock:
            mode = self._add.get("mode", "partial")
            storage_id = self._add.get("storage_id")
            account_id = self._add.get("account_id")
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
            stats = repo.commit_plan(conn, snapshot, plan, on_stage=on_stage,
                                     storage_id=storage_id,
                                     account_id=account_id)
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
        storage_label = None
        if storage_id:
            storage = storages_mod.get(conn, storage_id)
            storage_label = (storage or {}).get("label") or storage_id
        queued = 0
        picker, picker_total = [], 0
        if mode == "full" and playlist_id:
            # «Полная»: всё ожидающее в этом источнике встаёт в очередь -
            # в выбранную пользователем папку, а не в глобальную.
            queued = repo.enqueue_playlist(conn, playlist_id,
                                           storage_id=storage_id)
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
                        "storage_id": storage_id,
                        "storage_label": storage_label,
                        "account_id": account_id,
                        "account_label": (google_auth.account_labels()
                                          .get(account_id or "", "")),
                        "title": (snapshot["playlist"].get("title")
                                  or snapshot["playlist"].get("remote_id")),
                        "url": snapshot.get("url")})
            self._busy = False
            self._heavy_at = 0.0
            self._status = "Готово"
        self._log("Добавлено: плейлист {}, новых {}, связей {}{}{}".format(
            1, stats["new_videos"], stats["links_to_create"],
            f", в очередь {queued}" if queued else "",
            f", качать в «{storage_label}»" if storage_label else ""))

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
    #  Переупаковка по шаблону (M9)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _repack_selection(request) -> dict:
        """Что переупаковываем: явные строки, иначе текущую область."""
        request = request or {}
        try:
            ids = [int(v) for v in request.get("ids") or []]
        except (TypeError, ValueError):
            ids = []
        if ids:
            return {"ids": ids}
        scope = request.get("scope")
        if isinstance(scope, dict) and scope.get("type"):
            return {"scope": scope}
        return {"scope": {"type": "pool"}}

    def repack_preview(self, request=None) -> dict:
        """Превью: сколько переименуется, что конфликтует, что нельзя."""
        template = self._current_settings().get("output_template") or ""
        plan = repack_mod.plan_repack(self.db.conn,
                                      self._repack_selection(request), template)
        if plan.get("error"):
            return plan
        limit = repack_mod.DISPLAY_LIMIT
        return {"ok": True, "template": plan["template"],
                "selected": plan["selected"], "count": plan["count"],
                "rename": plan["rename"][:limit],
                "rename_total": plan["rename_total"],
                "unchanged": plan["unchanged"],
                "conflicts": plan["conflicts"][:limit],
                "conflict_total": len(plan["conflicts"]),
                "no_meta": plan["no_meta"][:limit],
                "no_meta_total": len(plan["no_meta"]),
                "bytes": plan["bytes"]}

    def repack_start(self, request=None) -> dict:
        """Начать переупаковку: план пересчитывается на старте."""
        with self._lock:
            if self._repack.get("running"):
                return {"error": "Переупаковка уже идёт"}
        template = self._current_settings().get("output_template") or ""
        plan = repack_mod.plan_repack(self.db.conn,
                                      self._repack_selection(request), template)
        if plan.get("error"):
            return plan
        if not plan["count"]:
            return {"error": "Нечего переупаковывать: всё уже по шаблону "
                             f"(без изменений: {plan['unchanged']})"}

        self._repack_stop.clear()
        with self._lock:
            self._repack = {"running": True, "done": 0,
                            "total": sum(1 + len(i["aux"])
                                         for i in plan["items"]),
                            "current": "", "summary": None, "error": None}
            self._busy = True
            self._status = f"Переупаковка {plan['count']} файл(ов)"
        self._log(f"Переупаковка: {plan['count']} к переименованию, "
                  f"конфликтов {len(plan['conflicts'])}, "
                  f"без метаданных {len(plan['no_meta'])}")
        thread = threading.Thread(target=self._repack_worker, args=(plan,),
                                  daemon=True, name="omnistash-repack")
        self._repack_thread = thread
        thread.start()
        return {"ok": True, "count": plan["count"],
                "conflict_total": len(plan["conflicts"]),
                "no_meta_total": len(plan["no_meta"])}

    def repack_stop(self) -> dict:
        """Прервать: уже переименованное остаётся, строки везде валидны."""
        with self._lock:
            running = self._repack.get("running")
        if not running:
            return {"ok": False}
        self._repack_stop.set()
        self._log("Переупаковка остановлена пользователем")
        return {"ok": True}

    def _repack_worker(self, plan: dict) -> None:
        def progress(done, total, current):
            with self._lock:
                self._repack.update(done=done, total=total, current=current)
                self._status = f"Переупаковка {done}/{total}"

        try:
            result = repack_mod.apply_repack(self.db.conn, plan,
                                             stop=self._repack_stop,
                                             progress=progress)
            summary = f"Переименовано {result['renamed']} из {result['total']}"
            if result["dirs_removed"]:
                summary += f", удалено пустых папок {result['dirs_removed']}"
            if result["errors"]:
                summary += f", не получилось {len(result['errors'])}"
            self._log(summary)
            for error in result["errors"][:5]:
                self._log("Переупаковка: " + error)
            self._finish_repack(summary, result.get("errors") or [])
        except repack_mod.RepackCancelled:
            self._finish_repack("Переупаковка остановлена - уже "
                                "переименованное осталось", [])
        except Exception as exc:  # noqa: BLE001 - фон не должен молча умереть
            self._log(f"Переупаковка упала: {exc}")
            self._finish_repack(f"Ошибка: {exc}", [str(exc)])

    def _finish_repack(self, summary: str, errors: list[str]) -> None:
        with self._lock:
            self._repack.update(running=False, summary=summary,
                                current="", errors=errors)
            self._busy = False
            self._status = "Готово"
            self._heavy_at = 0.0

    # ------------------------------------------------------------------ #
    #  Локальные пометки: теги, рейтинг, заметки, «просмотрено»
    # ------------------------------------------------------------------ #

    def save_fields(self, request=None) -> dict:
        """Записать пользовательские поля у одной строки или группы.

        Только whitelist (repo.save_fields): статус, хранилище и
        метаданные площадки отсюда изменить нельзя.
        """
        request = request or {}
        try:
            ids = [int(v) for v in request.get("ids") or []]
        except (TypeError, ValueError):
            return {"error": "Не выбрано видео"}
        if not ids:
            return {"error": "Не выбрано видео"}
        fields = request.get("fields") or {}
        try:
            updated = repo.save_fields(self.db.conn, ids, fields)
        except Exception as exc:  # noqa: BLE001
            self._log(f"Пометка не записалась: {exc}")
            return {"error": f"Не удалось записать: {exc}"}
        self._heavy_at = 0.0
        if len(ids) == 1:
            self._log("Пометка: " + ", ".join(f"{k}={v!r}"
                                              for k, v in fields.items()))
        else:
            self._log(f"Пометка для {len(ids)} строк: обновлено {updated}")
        return {"ok": True, "updated": updated}

    def get_thumb(self, request=None) -> dict:
        """Одна обложка как data-URI (для карточки видео)."""
        try:
            video_id = int((request or {}).get("id") or 0)
        except (TypeError, ValueError):
            return {"ok": False, "reason": "нет видео"}
        row = self.db.conn.execute(
            """SELECT path FROM files
                WHERE video_id=? AND kind='thumbnail' AND missing=0
                ORDER BY id LIMIT 1""", (video_id,)).fetchone()
        if not row:
            return {"ok": False, "reason": "обложка не скачана"}
        data = _thumb_data_uri(row["path"])
        if data is None:
            return {"ok": False,
                    "reason": "не прочиталась или слишком большая"}
        return {"ok": True, "data": data}

    def get_thumbs(self, request=None) -> dict:
        """Пачка обложек для плиток: {"thumbs": {video_id: data-URI}}.

        Один вызов моста вместо десятков: плитка просит превью, только
        когда попадает на экран (IntersectionObserver), но таких плиток
        всё равно много. id сверх лимита отклоняем честно - пусть UI
        дробит сам; битые и пропавшие файлы молча не попадают в ответ,
        плитка покажет заглушку.
        """
        request = request or {}
        try:
            ids = [int(v) for v in request.get("ids") or []]
        except (TypeError, ValueError):
            return {"error": "некорректный список id"}
        # dedupe с сохранением порядка, нули и мусор в сторону
        ids = [video_id for video_id in dict.fromkeys(ids) if video_id > 0]
        if not ids:
            return {"thumbs": {}}
        if len(ids) > THUMB_BATCH_LIMIT:
            return {"error": f"слишком много id за раз: {len(ids)} "
                             f"(максимум {THUMB_BATCH_LIMIT})"}
        best: dict[int, str] = {}
        for chunk_start in range(0, len(ids), 400):
            chunk = ids[chunk_start:chunk_start + 400]
            marks = ",".join("?" * len(chunk))
            for row in self.db.conn.execute(
                    f"""SELECT video_id, path FROM files
                         WHERE kind='thumbnail' AND missing=0
                           AND video_id IN ({marks})
                         ORDER BY video_id, id""", chunk):
                # у видео бывает несколько обложек - берём первую по id
                best.setdefault(row["video_id"], row["path"])
        thumbs = {}
        for video_id, path in best.items():
            data = _thumb_data_uri(path)
            if data:
                thumbs[str(video_id)] = data
        return {"thumbs": thumbs}

    # ------------------------------------------------------------------ #
    #  Открыть локально: системный плеер и проводник
    # ------------------------------------------------------------------ #

    def _video_file(self, video_id: int) -> tuple:
        """(путь к файлу видео, причина отказа). Путь - строго из индекса."""
        row = self.db.conn.execute(
            """SELECT path FROM files
                WHERE video_id=? AND kind='video' AND missing=0
                ORDER BY id LIMIT 1""", (video_id,)).fetchone()
        if not row:
            known = self.db.conn.execute(
                "SELECT COUNT(*) n FROM files WHERE video_id=? AND kind='video'",
                (video_id,)).fetchone()["n"]
            return None, ("файл пропал с диска - переиндексируйте"
                          if known else "видео не скачано")
        if not os.path.exists(row["path"]):
            return None, "файл пропал с диска - переиндексируйте"
        return row["path"], None

    def open_file(self, request=None) -> dict:
        """Открыть видео в системном плеере (двойной клик по плитке/строке)."""
        try:
            video_id = int((request or {}).get("id") or 0)
        except (TypeError, ValueError):
            return {"error": "нет видео"}
        path, error = self._video_file(video_id)
        if error:
            return {"error": error}
        try:
            # Windows: открыть ассоциированным приложением (плеер по умолчанию)
            os.startfile(path)
        except OSError as exc:
            return {"error": f"не удалось открыть: {exc}"}
        self._log(f"Открыто: {path}")
        return {"ok": True, "path": path}

    def open_folder(self, request=None) -> dict:
        """Открыть проводник с выделенным файлом видео."""
        try:
            video_id = int((request or {}).get("id") or 0)
        except (TypeError, ValueError):
            return {"error": "нет видео"}
        path, error = self._video_file(video_id)
        if error:
            return {"error": error}
        try:
            subprocess.Popen(["explorer", "/select,",
                              os.path.normpath(path)])
        except OSError as exc:
            return {"error": f"не удалось открыть папку: {exc}"}
        return {"ok": True, "path": path}

    # ------------------------------------------------------------------ #
    #  Проверка целостности (сверка файлов с хешем в индексе)
    # ------------------------------------------------------------------ #

    def verify_start(self, request=None) -> dict:
        """Сверить выбранные (или текущую область) файлы с их хешами."""
        with self._lock:
            if self._verify.get("running"):
                return {"error": "Проверка уже идёт"}
        self._verify_stop.clear()
        with self._lock:
            self._verify = {"running": True, "done": 0, "total": 0,
                            "current": "", "checked": 0, "filled": 0,
                            "broken": [], "broken_total": 0,
                            "missing": [], "missing_total": 0,
                            "summary": None, "error": None}
            self._busy = True
            self._status = "Проверка целостности…"
        selection = self._repack_selection(request)
        thread = threading.Thread(target=self._verify_worker,
                                  args=(selection,), daemon=True,
                                  name="omnistash-verify")
        self._verify_thread = thread
        thread.start()
        return {"ok": True}

    def verify_stop(self) -> dict:
        with self._lock:
            running = self._verify.get("running")
        if not running:
            return {"ok": False}
        self._verify_stop.set()
        self._log("Проверка целостности остановлена")
        return {"ok": True}

    def repair_broken(self) -> dict:
        """Поставить битые в очередь: файлы будут скачаны заново.

        Ключевой момент: yt-dlp счёл бы их уже скачанными, поэтому строке
        заранее пишется причина с префиксом CHECKSUM_PREFIX - по ней
        очередь включает перезапись.
        """
        with self._lock:
            broken = [row["video_id"] for row in self._verify.get("broken") or []]
        if not broken:
            return {"error": "Ремонтировать нечего"}
        conn = self.db.conn
        message = (repo.CHECKSUM_PREFIX +
                   " не совпала при проверке целостности")
        for video_id in broken:
            repo.set_status(conn, [video_id], "failed")
            repo.set_last_error(conn, video_id, message)
        queued = repo.enqueue(conn, broken)
        with self._lock:
            self._heavy_at = 0.0
        self._log(f"Ремонт: {queued} битых поставлено в очередь "
                  "(файлы будут перезаписаны)")
        if queued:
            self.dl.start()
        return {"queued": queued}

    def _verify_worker(self, selection: dict) -> None:
        def progress(done, total, current):
            with self._lock:
                self._verify.update(done=done, total=total, current=current)
                self._status = f"Проверка {done}/{total}"

        try:
            result = verify_mod.verify(self.db.conn, selection,
                                       progress=progress,
                                       stop=self._verify_stop)
        except Exception as exc:  # noqa: BLE001 - фон не должен молча умереть
            self._log(f"Проверка упала: {exc}")
            with self._lock:
                self._verify.update(running=False, error=str(exc),
                                    summary=f"Ошибка: {exc}")
                self._busy = False
            return

        if result.get("error"):
            summary = result["error"]
        else:
            summary = (f"Проверено {result['checked']} из {result['total']}"
                       f" · битых {result['broken_total']}"
                       f" · без хеша {result['filled']}"
                       f" · нет файла {result['missing_total']}")
            if result.get("stopped"):
                summary = "Остановлено: " + summary
        with self._lock:
            self._verify.update(
                running=False, checked=result.get("checked", 0),
                filled=result.get("filled", 0),
                broken=result.get("broken") or [],
                broken_total=result.get("broken_total", 0),
                missing=result.get("missing") or [],
                missing_total=result.get("missing_total", 0),
                summary=summary, error=result.get("error"), current="")
            self._busy = False
            self._status = "Готово"
            self._heavy_at = 0.0
        self._log(summary)

    # ------------------------------------------------------------------ #
    #  Обновление метаданных: «подтянуть актуальные данные по площадке»
    # ------------------------------------------------------------------ #

    def refresh_meta_start(self, request=None) -> dict:
        """Обойти все площадочные видео полным запросом и освежить строки.

        Это кнопка «обновить данные у уже скачанного»: файлы не трогаются,
        меняются только колонки (лайки, комменты, даты, обложки) и
        synced_at. Плоский синк таких полей не приносит - здесь приходят.
        """
        with self._lock:
            if self._refresh.get("running"):
                return {"error": "Обновление метаданных уже идёт"}
            if self._sync.get("running"):
                return {"error": "Идёт синхронизация - дождитесь, она сама "
                                 "обновит метаданные источников"}
        rows = [dict(r) for r in self.db.conn.execute(
            """SELECT id, remote_id, platform, webpage_url FROM videos
                WHERE platform <> 'local'
                ORDER BY id""")]
        if not rows:
            return {"error": "Обновлять нечего: площадочных видео нет"}
        self._refresh_stop.clear()
        with self._lock:
            self._refresh = {"running": True, "done": 0, "total": len(rows),
                             "current": "", "errors": 0, "summary": None}
            self._busy = True
            self._status = "Обновление метаданных…"
        thread = threading.Thread(target=self._refresh_worker, args=(rows,),
                                  daemon=True, name="omnistash-refresh-meta")
        with self._lock:
            self._refresh_thread = thread
        thread.start()
        self._log(f"Обновление метаданных: {len(rows)} видео "
                  "(без перекачки файлов)")
        return {"ok": True, "total": len(rows)}

    def refresh_meta_stop(self) -> dict:
        """Остановить обход: уже освежённые строки остаются."""
        with self._lock:
            if not self._refresh.get("running"):
                return {"ok": False}
        self._refresh_stop.set()
        self._log("Обновление метаданных остановлено")
        return {"ok": True}

    def _restore_unavailable(self, conn, video_id: int) -> None:
        """Снять метку «удалено на площадке», когда видео снова отдаётся.

        Файл остаётся скачанным, если был скачан (status='downloaded'),
        иначе строка снова «в индексе» (status='known'). Ничего не
        перекачивается - меняется только пометка.
        """
        row = conn.execute("SELECT status FROM videos WHERE id=?",
                           (video_id,)).fetchone()
        if not row or row["status"] != "unavailable":
            return
        has_file = conn.execute(
            "SELECT 1 FROM files WHERE video_id=? AND kind='video' "
            "AND missing=0 LIMIT 1", (video_id,)).fetchone()
        repo.set_status(conn, [video_id],
                        "downloaded" if has_file else "known")
        self._log(f"Видео снова доступно - метка «удалено» снята "
                  f"(id {video_id})")

    def _refresh_worker(self, rows: list[dict], *, final: bool = True) -> None:
        """Обход метаданных. final=False - этап внутри синхронизации
        (busy и статус дороги оставляет своему воркеру)."""
        conn = self.db.conn
        config = self._current_settings()
        done = errors = consecutive = 0
        stopped = False
        total = len(rows)
        for row in rows:
            if self._refresh_stop.is_set():
                stopped = True
                break
            url = row["webpage_url"] or _fallback_url(row)
            current = ""
            try:
                info = sources.fetch_video(url, settings=config,
                                           stop=self._refresh_stop)
                data = normalize_video(info,
                                       platform=row.get("platform")
                                       or "youtube", origin="yt-dlp")
                vid, _created = repo.upsert_video(conn, data, full=True)
                if vid:
                    # Данные только что пришли с площадки - вот честное
                    # «время синка» для этой строки.
                    conn.execute("UPDATE videos SET synced_at=? WHERE id=?",
                                 (now_iso(), vid))
                    # Если строка была помечена «удалено» - метка снимается.
                    self._restore_unavailable(conn, vid)
                current = data.get("title") or row["remote_id"]
                consecutive = 0
            except Exception as exc:  # noqa: BLE001 - строка не валит обход
                errors += 1
                consecutive += 1
                current = f"ошибка: {exc}"
                if downloader_mod.unavailable_error(str(exc)):
                    # Удалено/приватное: помечаем строку архивной и НИЧЕГО
                    # не пишем поверх - последние известные данные (raw_json,
                    # колонки, файл) остаются как были.
                    try:
                        repo.set_status(conn, [row["id"]], "unavailable")
                        self._log(f"{row['remote_id'] or row['id']}: "
                                  "удалено на площадке - строка помечена, "
                                  "данные сохранены")
                    except Exception:  # noqa: BLE001 - пометка не должна валить обход
                        pass
                elif consecutive <= 3 or consecutive % 20 == 0:
                    self._log(f"Метаданные {row['remote_id']}: {exc}")
                if consecutive >= 3 and done < 2:
                    # С первых же строк ничего не идёт - связи нет, дальше
                    # мучать площадку бессмысленно.
                    self._log("Обновление метаданных прервано: "
                              "площадка не отвечает с самого начала")
                    stopped = True
                    break
            done += 1
            with self._lock:
                self._refresh.update(done=done, current=current, errors=errors)
                self._status = f"Метаданные {done}/{total}"
                if self._sync.get("meta_active"):
                    # Этап 2 синка: счётчики дублируются в состояние sync,
                    # чтобы вкладка «Источники» показывала прогресс этапа.
                    self._sync["meta"] = {"done": done, "total": total,
                                          "current": current,
                                          "errors": errors}

        summary = (f"Метаданные обновлены: {done} из {total}"
                   f" · ошибок {errors}")
        if stopped:
            summary = "Остановлено: " + summary
        with self._lock:
            self._refresh.update(running=False, summary=summary, current="")
            if final:
                self._busy = False
                self._status = "Готово"
            self._heavy_at = 0.0
        self._log(summary)

    # ------------------------------------------------------------------ #
    #  Mesh: запрос видео у участников (кнопка в карточке)
    # ------------------------------------------------------------------ #

    def mesh_fetch_video(self, request=None) -> dict:
        """Восстановить файл у участников - путь восстановления из карточки.

        Та же честная механика, что в очереди (mesh.fetch.restore): манифесты
        -> стоп-лист/эталон -> перенос -> sha256 -> структура -> провенанс.
        """
        request = request or {}
        try:
            video_id = int(request.get("id") or 0)
        except (TypeError, ValueError):
            return {"error": "Видео не указано"}
        config = dict(self._current_settings())
        if not config.get("mesh_enabled"):
            return {"error": "Участие в mesh выключено в настройках "
                             "(секция Mesh)"}
        with self._lock:
            if self._mesh.get("running"):
                return {"error": "Запрос к участникам уже идёт"}
        conn = self.db.conn
        row = conn.execute(
            "SELECT id, remote_id, title, key, status, channel_id, "
            "target_storage_id FROM videos WHERE id=?", (video_id,)).fetchone()
        if row is None:
            return {"error": "Видео не найдено"}
        row = dict(row)
        if row["status"] in ("queued", "downloading"):
            return {"error": "Видео уже качается очередью"}
        if row["status"] == "downloaded":
            return {"error": "Файл уже скачан - проверяйте в «Целостности»"}
        storage, error = storages_mod.resolve_for_row(conn, row, config)
        if not storage:
            return {"error": error}
        self._mesh_stop.clear()
        with self._lock:
            self._mesh = {"running": True, "video_id": video_id,
                          "title": row.get("title") or "", "error": None,
                          "summary": None}
        thread = threading.Thread(target=self._mesh_worker,
                                  args=(row, config, storage["path"]),
                                  name="omnistash-mesh-fetch", daemon=True)
        with self._lock:
            self._mesh_thread = thread
        thread.start()
        self._log(f"Mesh: запрос «{row.get('title')}» у участников "
                  f"(назначение: {storage['label']})")
        return {"ok": True, "title": row.get("title") or ""}

    def mesh_fetch_stop(self) -> dict:
        """Остановить идущий запрос (перенос проверяет stop на чанках)."""
        with self._lock:
            if not self._mesh.get("running"):
                return {"ok": False}
        self._mesh_stop.set()
        self._log("Mesh: запрос остановлен")
        return {"ok": True}

    def mesh_reject(self, request=None) -> dict:
        """«Файл оказался не тем»: стоп-лист + удаление негодной копии.

        Один клик из карточки (с подтверждением - там же): хеш уходит в
        стоп-лист, ключ-носитель запоминается как нарушитель (репутация),
        сам файл удаляется с диска, строка files остаётся с missing=1
        (хеш - для истории), статус становится «упавшее» - «Повторить
        упавшие» пойдёт с площадки, а не с участников.
        """
        request = request or {}
        try:
            video_id = int(request.get("id") or 0)
        except (TypeError, ValueError):
            return {"error": "Видео не указано"}
        conn = self.db.conn
        row = conn.execute(
            "SELECT id, title, status FROM videos WHERE id=?",
            (video_id,)).fetchone()
        if row is None:
            return {"error": "Видео не найдено"}
        if row["status"] != "downloaded":
            return {"error": "Отзыв возможен у скачанного файла "
                             "(сейчас статус: "
                             f"{row['status']})"}
        mesh = repo.mesh_file(conn, video_id)
        if mesh is None:
            return {"error": "Это не mesh-копия: отзыв относится только к "
                             "файлам, полученным от участников"}
        digest = mesh["hash"]
        if not digest:
            return {"error": "У файла нет sha256 - отзывать нечего"}
        if mesh["missing"]:
            return {"error": "Файл уже отсутствует на диске"}
        meta = mesh["meta"]
        peer = str(meta.get("peer") or "")
        # 1) стоп-лист: решение человека окончательно - хеш негоден,
        #    даже если удалять файл не выйдет (права, диск).
        try:
            mesh_feedback.ban(conn, digest, peer,
                              note="несовпадение после просмотра")
        except ValueError as exc:
            return {"error": str(exc)}
        # 2) сам негодный файл - с диска (иначе он останется «скачанным»).
        remove_note = ""
        path = str(mesh["path"] or "")
        if path:
            try:
                if os.path.exists(path):
                    os.remove(path)
                conn.execute("UPDATE files SET missing=1 WHERE path=?",
                             (path,))
            except OSError as exc:
                remove_note = (f"; сам файл удалить не удалось ({exc}) - "
                               "удалите вручную")
        repo.set_status(conn, [video_id], "failed")
        repo.set_last_error(
            conn, video_id,
            "негодная копия из mesh: хеш в стоп-листе - повторите с площадки"
            + remove_note)
        self._heavy_at = 0.0
        shown = str(digest)
        self._log(f"Mesh: «{row['title']}» отклонён - хеш "
                  f"{shown[:18]}… в стоп-листе"
                  + (f", носитель {peer[:12]}…" if peer else "")
                  + remove_note)
        return {"ok": True, "title": row["title"], "removed": not remove_note,
                "hash": shown}

    # ------------------------------------------------------------------ #
    #  B1: профиль участника и поиск по нику (каталог имён DHT)
    # ------------------------------------------------------------------ #

    def peer_profile(self, request=None) -> dict:
        """Мой профиль для диалога «Участники» (+ статус для диагностики).

        Ключ идентичности создаётся здесь при первом открытии (а не при
        каждом старте окна): участнику, который никогда не включал mesh,
        файл ключа не нужен.
        """
        _key, pub = mesh_identity.get_identity()
        self._truststore_seed()      # пины могли прийти с прошлого раза
        config = self._current_settings()
        nick = mesh_dht.clean_nick(config.get("mesh_nick"))
        node = self._mesh_dht
        repo_peer_health = repo.peer_health(self.db.conn)   # 1.4
        status = {
            "dht": bool(node is not None and node.running),
            "nodes": len(node.table) if node is not None else 0,
            "external_ip": (node.external_ip
                            if node is not None else None) or "",
            "hello_rx": int(getattr(node, "hello_rx", 0)
                            if node is not None else 0),
            "nick_last": dict(self._nick_last or {}),
            "upnp": (self._nat.state().get("reason", "")
                     if self._nat is not None else "выключен (настройка)"),
            "mdns": {"available": mesh_mdns.available(),
                     "running": bool(self._mdns_browser is not None
                                     and self._mdns_browser.running),
                     "found": len(self._mdns_found)},
        }
        return {"pubkey": pub.hex(),
                "fingerprint": mesh_identity.fingerprint(pub),
                "nick": nick,
                "nick_valid": mesh_dht.nick_ok(nick),
                "enabled": bool(config.get("mesh_enabled")),
                "strict": bool(config.get("mesh_strict")),
                "dht": status["dht"],
                "devices": [
                    {**device,
                     "path": mesh_dht.path_class(device.get("last_addr")
                                                 or ""),
                     "health": (repo_peer_health.get(device["pubkey"])
                                or {}).get("status"),
                     "health_at": (repo_peer_health.get(device["pubkey"])
                                   or {}).get("checked_at")}
                    for device in repo.truststore_list(self.db.conn)],
                "catalogs": repo.catalog_origins(self.db.conn),
                "status": status}

    def nick_search(self, request=None) -> dict:
        """Поиск участников по нику: каталог имён в DHT (B1).

        Точное совпадение без регистра - DHT не умеет частичных
        запросов (UI об этом пишет). Сеть отдаёт АДРЕСА; личность
        каждого адресата устанавливается здесь же - быстрым открытием
        TLS-канала (аттестация ключа, без пинов и без запросов видео):
        в результатах приходят отпечаток и метка «ключ запомнен».
        Коллизии (одни ник у разных ключей) - честно списком.
        """
        request = request or {}
        query = mesh_dht.clean_nick(request.get("query"))
        if not mesh_dht.nick_ok(query):
            return {"error": f"Введите ник от {mesh_dht.NICK_MIN} до "
                             f"{mesh_dht.NICK_MAX} символов: буквы, цифры, "
                             "пробел, дефис, подчёркивание"}
        if not self._current_settings().get("mesh_enabled"):
            return {"error": "Участие в mesh выключено в настройках "
                             "(секция Mesh)"}
        node = self._mesh_dht
        if node is None or not node.running:
            return {"error": "DHT-узел не поднят: включите участие и "
                             "дождитесь старта (журнал)"}
        if not len(node.table):
            return {"error": "DHT ещё набирает узлов (первые секунды "
                             "после старта) - повторите поиск"}
        found = node.name_lookup_sync(query)
        # F1b: пробы ПАРАЛЛЕЛЬНО и с коротким таймаутом - адресов теперь
        # несколько (LAN + VPN + публичный), мёртвые не должны копить
        # секунды последовательного ожидания (8x6с = 48с было).
        def check(item):
            addr = str(item.get("addr") or "")
            if not addr:
                return None
            try:
                # pin=False: поиск НЕ пишет пины - их оставляет явная
                # кнопка «Проверить адрес» (файл знакомств не растёт от
                # одного лишь любопытства).
                ident = mesh_client.probe(addr, expect=None, pin=False,
                                          timeout=2.5)
            except Exception:  # noqa: BLE001 - не отвечает/не наш канал
                return None
            if not ident:
                return None
            return {
                "nick": query,
                "addr": addr,
                "pubkey": ident,
                "fingerprint": mesh_identity.fingerprint(ident),
                "pinned": mesh_channel.pin_get(addr) == ident,
            }

        results = []
        batch = found[:8]
        if batch:
            with ThreadPoolExecutor(max_workers=min(8, len(batch))) as pool:
                for res in pool.map(check, batch):
                    if res:
                        results.append(res)
        self._log(f"Mesh: поиск «{query}» по каталогу имён - "
                  f"адресов {len(found)}, с ключом {len(results)}")
        # found - сколько вернула сеть (включая мусор публичного DHT):
        # без этого числа «ничего не найдено» не отличить от «адреса
        # есть, но канал/TLS их отсеял» - две РАЗНЫЕ болезни.
        return {"ok": True, "query": query, "results": results,
                "found": len(found)}

    def nick_probe(self, request=None) -> dict:
        """Проверить адрес кандидата: TLS-канал + аттестация ключа (B1).

        Ничего не качаем и id не отправляем: проверка в connect идёт до
        первого байта. Если ключа адресата раньше не было - первый
        контакт запомнит его (TOFU, mesh_pins.json). Полная сверка «12
        словами» - этап B2.
        """
        request = request or {}
        addr = str(request.get("addr") or "").strip()
        pubkey = str(request.get("pubkey") or "").strip().lower()
        if not addr or ":" not in addr:
            return {"error": "Адрес не указан (ожидается ip:порт)"}
        if not self._current_settings().get("mesh_enabled"):
            return {"error": "Участие в mesh выключено в настройках "
                             "(секция Mesh)"}
        had_pin = bool(mesh_channel.pin_get(addr))
        try:
            identity_hex = mesh_client.probe(addr, expect=pubkey or None)
        except mesh_channel.ChannelError as exc:
            return {"error": str(exc)}
        except TimeoutError:
            # F3: молчание ≠ отказ - это отдельная диагноз-ветка.
            return {"error": f"{addr} молчит: приложение на той стороне "
                             "не запущено ИЛИ трафик фильтруется "
                             "(DPI/zapret/брандмауэр). Выключать их не "
                             "нужно: попробуйте VPN-адрес пира "
                             "(Tailscale) или добавьте исключение для "
                             "нашего порта в DPI-программе."}
        except Exception as exc:  # noqa: BLE001 - адрес мог умереть
            return {"error": f"{addr} не отвечает: {exc}"}
        pinned = mesh_channel.pin_get(addr) == identity_hex
        # Ключ, открывший канал, - устройство в хранилище доверия: свежий
        # адрес запоминается всегда, а решения человека (сверка/отзыв)
        # переписываются только им.
        device = repo.truststore_add(self.db.conn, identity_hex, addr=addr)
        try:
            repo.peer_addr_add(self.db.conn, identity_hex, addr, "probe")
        except ValueError:
            pass                            # кривой ключ не должен валить
        self._log(f"Mesh: {addr} - канал открыт, ключ "
                  f"{identity_hex[:16]}… "
                  + ("(первый контакт - запомнен)" if not had_pin
                     else "(ключ запомнен)")
                  + f" [доверие: {device['status']}]")
        return {"ok": True, "identity": identity_hex,
                "fingerprint": mesh_identity.fingerprint(identity_hex),
                "pinned": pinned, "new_pin": pinned and not had_pin,
                "trust": device["status"], "label": device["label"],
                "revoked": bool(device["revoked_at"])}

    def _strict_autoload(self, device: dict) -> None:
        """Strict включается при ПЕРВОМ подтверждённом устройстве (1.2).

        Повторные подтверждения НЕ включают режим, выключенный человеком:
        авто-включение работает, только пока в truststore не было
        подтверждённых устройств до этой операции.
        """
        try:
            others = repo.truststore_verified(self.db.conn) - {
                str(device.get("pubkey") or "")}
            if others or self._current_settings().get("mesh_strict"):
                return
            settings.set_value("mesh_strict", True)
            with self._lock:
                self._settings_rev += 1
                self._settings_cache.clear()
            settings.reload_if_changed(self._settings_cache)
            self._log("Mesh: strict включён автоматически - первое "
                      "подтверждённое устройство (выключается явно в "
                      "настройках, секция Mesh)")
        except Exception as exc:  # noqa: BLE001 - авто-включение не критично
            self._log(f"Mesh: strict не удалось включить: {exc}")

    def _truststore_seed(self) -> None:
        """Перенос TOFU-пинов адресов в устройства (идемпотентно, один раз).

        mesh_pins.json остаётся транспортным кэшем «адрес -> ключ», но
        человеку нужен список устройств - заводим его из уже запомненного.
        Сбой посева не должен мешать старту окна.
        """
        try:
            imported = repo.truststore_import_pins(self.db.conn,
                                                   mesh_channel.pins())
            if imported:
                self._log(f"Mesh: перенесено пинов в хранилище доверия "
                          f"(устройства): {imported}")
        except Exception as exc:  # noqa: BLE001 - старт окна важнее посева
            self._log(f"Mesh: перенос пинов в устройства не удался: {exc}")

    def trust_op(self, request=None) -> dict:
        """Операции над устройством: verify / revoke / restore / label / forget.

        «Подтвердить» - решение ЧЕЛОВЕКА после сверки отпечатка (при
        личном общении или по видеосвязи): программа личность не
        доказывает, она фиксирует решение и держит барьер в mesh.fetch
        (отзыв отсекает манифесты и файлы устройства, включая будущие).
        """
        request = request or {}
        op = str(request.get("op") or "")
        pubkey = str(request.get("pubkey") or "")
        labels = {"verify": "подтверждено", "revoke": "доверие отозвано",
                  "restore": "отзыв снят (TOFU)", "label": "имя изменено"}
        try:
            if op == "verify":
                device = repo.truststore_verify(self.db.conn, pubkey)
            elif op == "revoke":
                device = repo.truststore_revoke(self.db.conn, pubkey)
            elif op == "restore":
                device = repo.truststore_restore(self.db.conn, pubkey)
            elif op == "label":
                device = repo.truststore_set_label(
                    self.db.conn, pubkey, str(request.get("label") or ""))
            elif op == "forget":
                forgotten = repo.truststore_forget(self.db.conn, pubkey)
                # Пины адресов этого ключа снимаем тоже: иначе файл
                # знакомств высевал бы устройство заново при каждом
                # открытии диалога.
                pins_dropped = (mesh_channel.pin_del_key(pubkey)
                                if forgotten else 0)
                if forgotten:
                    self._log(f"Mesh: устройство {pubkey[:16]}… забыто "
                              f"(снято пинов: {pins_dropped}); при новом "
                              "контакте вернётся как TOFU")
                return {"ok": True, "forgotten": forgotten,
                        "pins_dropped": pins_dropped}
            else:
                return {"error": f"Неизвестная операция: {op!r}"}
        except ValueError as exc:
            return {"error": str(exc)}
        if op == "verify":
            self._strict_autoload(device)
        self._log(f"Mesh: устройство {pubkey[:16]}… - {labels.get(op, op)}")
        return {"ok": True, "device": device}

    # ------------------------------------------------------------------ #
    #  1.1: SAS-верификация «12 слов» (B2+S)
    # ------------------------------------------------------------------ #

    def trust_sas_begin(self, request=None) -> dict:
        """Начать SAS-сверку: вернуть показ выбранного формата.

        method выбирает инициатор (words/emoji/icons/digits); показ
        бывает строкой (digits), списком (words/emoji) или списком SVG
        (icons) - UI рисует что пришло. Пин не пишется: устройство
        подтверждается только после сверки человеком (trust_sas_confirm).
        """
        request = request or {}
        addr = str(request.get("addr") or "").strip()
        method = str(request.get("method") or "words").strip()
        if not addr or ":" not in addr:
            return {"error": "Адрес не указан (ожидается ip:порт)"}
        try:
            result = mesh_client.sas_begin(addr, method=method)
        except mesh_channel.ChannelError as exc:
            return {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - адрес мог умереть
            return {"error": f"SAS не начался: {exc}"}
        shown_svg = (result["shown"] if result["method"] == "icons"
                     else None)
        if result["method"] == "icons":
            shown = f"{len(result['shown'])} иконок"
        else:
            shown = " ".join(str(x) for x in result["shown"])
        with self._lock:
            self._sas_pending = {"pubkey": result["pubkey"], "addr": addr,
                                 "ts": time.time()}
        try:
            repo.peer_addr_add(self.db.conn, result["pubkey"], addr, "sas")
        except ValueError:
            pass                            # кривой ключ - не валить сверку
        self._log(f"SAS[{result['method']}]: сверьте на устройстве "
                  f"собеседника ({addr}): {shown}")
        return {"ok": True, **result, "shown_svg": shown_svg}

    def trust_sas_confirm(self, request=None) -> dict:
        """Человек подтвердил совпадение слов - устройство verified."""
        request = request or {}
        pubkey = str(request.get("pubkey") or "").strip().lower()
        with self._lock:
            pending = dict(self._sas_pending or {})
        if not pending or pending.get("pubkey") != pubkey:
            return {"error": "Нет активной SAS-сверки с этим ключом - "
                             "начните заново («Подтвердить 12 слов»)"}
        if time.time() - pending.get("ts", 0.0) > 600:
            return {"error": "Сверка устарела (10 минут) - начните заново"}
        device = repo.truststore_verify(self.db.conn, pubkey, via="sas")
        with self._lock:
            self._sas_pending = None
        self._log(f"SAS: устройство {pubkey[:16]}… подтверждено сверкой "
                  "12 слов")
        self._strict_autoload(device)
        return {"ok": True, "device": device}

    # ------------------------------------------------------------------ #
    #  LEG: юридические согласия (ACCEPTABLE_USE, права на файл)
    # ------------------------------------------------------------------ #

    def legal_state(self, request=None) -> dict:
        """Факты согласий человека (LEG-1/LEG-3): принимались ли правила
        и предупреждение о передаче файлов другим лицам."""
        return {"ok": True, **legal_mod.state()}

    def legal_accept(self, request=None) -> dict:
        """Зафиксировать согласие: человек явно нажал «Принять».

        Факт (когда) пишется отдельно от настроек (app.legal): юридическая
        рамка требует датированного согласия, а не молчаливого.
        """
        request = request or {}
        data = legal_mod.accept(aup=bool(request.get("aup")),
                                first_send=bool(request.get("first_send")),
                                adult=bool(request.get("adult")))
        if request.get("aup"):
            self._log("Правила использования (docs/ACCEPTABLE_USE.md) "
                      "приняты")
        if request.get("adult"):
            self._log("Подтверждено: пользователю есть 18 "
                      "(возрастные площадки разрешены)")
        if request.get("first_send"):
            self._log("Предупреждение о передаче принято: файлы уходят "
                      "другим лицам - только в рамках ваших прав")
        return {"ok": True, **data}

    # ------------------------------------------------------------------ #
    #  Этап 1: диагностика устройства (doctor, 1.5)
    # ------------------------------------------------------------------ #

    def device_doctor(self, request=None) -> dict:
        """Диагностика устройства за один прогон (1.5): ключ/адрес/канал.

        Только чтение и ОДИН TLS-probe (pin=False): статус доверия,
        отвечает ли канал и с каким ключом, что видит DHT и сколько
        строк каталога устройства в зеркале. Файлы и состояния не
        меняются - чисто диагностика для человека.
        """
        request = request or {}
        pubkey = str(request.get("pubkey") or "").strip().lower()
        device = repo.truststore_get(self.db.conn, pubkey) if pubkey \
            else None
        if not device:
            return {"error": "Устройство не найдено в хранилище доверия"}
        name = device.get("label") or f"{pubkey[:16]}…"
        lines = [f"Доктор «{name}» [{device.get('status')}]"]
        addr = str(device.get("last_addr") or "")
        reachable = None
        if addr:
            try:
                ident = mesh_client.probe(addr, expect=pubkey, pin=False,
                                          timeout=2.5)
                reachable = bool(ident)
                lines.append(f"канал {addr}: ОТКРЫТ, ключ "
                             f"{mesh_identity.fingerprint(ident)}")
            except mesh_channel.ChannelError as exc:
                reachable = False
                lines.append(f"канал {addr}: ОТКАЗ - {exc}")
            except TimeoutError:
                reachable = False
                lines.append(f"канал {addr}: МОЛЧАНИЕ - приложение не "
                             "запущено ИЛИ трафик фильтруется "
                             "(DPI/брандмауэр)")
            except Exception as exc:  # noqa: BLE001 - диагностика не валит
                reachable = False
                lines.append(f"канал {addr}: ошибка - {exc}")
        else:
            lines.append("адрес неизвестен: нет последнего контакта")
        node = mesh_dht.current()
        if node is not None and node.running:
            lines.append(f"DHT: узлов {len(node.table)}, внешний IP "
                         f"{node.external_ip or 'не выучен'}")
        else:
            lines.append("DHT: не поднят")
        count = self.db.conn.execute(
            "SELECT COUNT(*) AS c FROM mesh_catalog WHERE origin=?",
            (pubkey,)).fetchone()["c"]
        lines.append(f"каталог: строк в зеркале {int(count)}")
        self._log("Доктор: " + "; ".join(lines))
        return {"ok": True, "report": "\n".join(lines),
                "reachable": reachable, "catalog_rows": int(count)}

    # ------------------------------------------------------------------ #
    #  B3: зеркало каталогов участников (синхронизация индекса)
    # ------------------------------------------------------------------ #

    def _catalog_addresses(self) -> list[tuple[str, str | None]]:
        """[(addr, ожидаемый ключ)] из mesh_peers и устройств truststore.

        Для устройств адрес берётся с ключом (канал обязан открыться
        именно им); отозванные устройства каталогов не опрашиваются.
        """
        out: list[tuple[str, str | None]] = []
        seen: set[str] = set()
        config = self._current_settings()
        for entry in str(config.get("mesh_peers") or "") \
                .replace(";", ",").split(","):
            entry = entry.strip()
            if not entry:
                continue
            addr, _, expect = entry.partition("@")
            addr = addr.strip()
            if not addr or addr in seen:
                continue
            seen.add(addr)
            out.append((addr, expect.strip().lower() or None))
        for device in repo.truststore_list(self.db.conn):
            addr = str(device.get("last_addr") or "")
            if addr and addr not in seen and device["status"] != "revoked":
                seen.add(addr)
                out.append((addr, device["pubkey"]))
        # mDNS-находки (B4): адрес есть, ключ пока нет - без ожидания
        with self._lock:
            mdns_addrs = [f"{addr}:{info['port']}"
                          for addr, info in self._mdns_found.items()]
        for addr in mdns_addrs:
            if addr not in seen:
                seen.add(addr)
                out.append((addr, None))
        return out

    def catalog_refresh(self, request=None) -> dict:
        """Обновить зеркала каталогов у участников (B3).

        Подпись каталога и ключ канала сверяются в mesh.client; здесь -
        применение LWW-зеркала (версии строк сравниваются только внутри
        одного узла) и журнал по каждому адресату.
        """
        if not self._current_settings().get("mesh_enabled"):
            return {"error": "Участие в mesh выключено в настройках "
                             "(секция Mesh)"}
        pairs = self._catalog_addresses()
        if not pairs:
            return {"error": "Нет адресов участников: добавьте "
                             "mesh_peers в настройках Mesh или проверьте "
                             "адрес устройства"}
        expects = {addr: key for addr, key in pairs if key}
        try:
            found = mesh_client.collect_catalogs([a for a, _k in pairs],
                                                 expects=expects or None)
        except Exception as exc:  # noqa: BLE001 - сбой не должен ронять окно
            return {"error": f"Каталоги не получены: {exc}"}
        summary = []
        for addr, pubkey, entries in found:
            stats = repo.catalog_apply(self.db.conn, pubkey, entries,
                                       addr=addr)
            # Автор каталога - устройство: запомнить адрес не помешает
            # (решения человека не переписываются - см. truststore_add).
            repo.truststore_add(self.db.conn, pubkey, addr=addr)
            try:
                repo.peer_addr_add(self.db.conn, pubkey, addr, "catalog")
            except ValueError:
                pass                        # кривой ключ - не валить
            summary.append({"addr": addr, "pubkey": pubkey,
                            "fingerprint": mesh_identity.fingerprint(pubkey),
                            **stats})
            self._log(f"Mesh: каталог {addr} ({len(entries)} строк): "
                      f"+{stats['added']} ~{stats['updated']} "
                      f"-{stats['removed']}")
        if not summary:
            return {"error": "Ни один участник не ответил каталогом "
                             "(адреса недоступны или старая версия) - "
                             "см. журнал"}
        return {"ok": True, "fetched": summary}

    def catalog_list(self, request=None) -> dict:
        """Зеркало для диалога: строки с автором и признаком «есть у меня»."""
        request = request or {}
        rows = repo.catalog_list(self.db.conn,
                                 query=str(request.get("query") or ""),
                                 limit=int(request.get("limit") or 200))
        for row in rows:
            own = repo.catalog_own(self.db.conn, row["remote_id"])
            row["own_id"] = own["id"] if own else None
            row["own_status"] = own["status"] if own else None
        return {"ok": True, "rows": rows,
                "origins": repo.catalog_origins(self.db.conn)}

    def catalog_restore(self, request=None) -> dict:
        """Восстановить файл строки из каталога участника (mesh.fetch).

        Только по СВОЕЙ строке - продуктовое правило: чужие метаданные не
        принимаются, сверять нечего. Канал привязывается ключом автора
        каталога (mesh_peers = addr@origin): запрос уйдёт только по
        подтверждённой личности.
        """
        request = request or {}
        remote_id = str(request.get("remote_id") or "")
        origin = str(request.get("origin") or "")
        addr = str(request.get("addr") or "")
        if not remote_id or not origin or not addr:
            return {"error": "Не указаны строка, устройство и адрес"}
        own = repo.catalog_own(self.db.conn, remote_id)
        if not own:
            return {"error": "Такой строки нет в вашем индексе: сначала "
                             "добавьте источник и синхронизируйте его"}
        config = dict(self._current_settings())
        if not config.get("mesh_enabled"):
            return {"error": "Участие в mesh выключено в настройках "
                             "(секция Mesh)"}
        with self._lock:
            if self._mesh.get("running"):
                return {"error": "Запрос к участникам уже идёт"}
        if own["status"] in ("queued", "downloading"):
            return {"error": "Видео уже качается очередью"}
        if own["status"] == "downloaded":
            return {"error": "Файл уже скачан - проверяйте в «Целостности»"}
        conn = self.db.conn
        row = dict(conn.execute(
            "SELECT id, remote_id, title, key, status, channel_id, "
            "target_storage_id FROM videos WHERE id=?",
            (own["id"],)).fetchone())
        storage, error = storages_mod.resolve_for_row(conn, row, config)
        if not storage:
            return {"error": error}
        config["mesh_peers"] = f"{addr}@{origin}"
        self._mesh_stop.clear()
        with self._lock:
            self._mesh = {"running": True, "video_id": row["id"],
                          "title": row.get("title") or "", "error": None,
                          "summary": None}
        thread = threading.Thread(target=self._mesh_worker,
                                  args=(row, config, storage["path"]),
                                  name="omnistash-catalog-fetch", daemon=True)
        with self._lock:
            self._mesh_thread = thread
        thread.start()
        self._log(f"Mesh: восстановление из каталога {addr[:24]}… "
                  f"«{row.get('title')}» (назначение: {storage['label']})")
        return {"ok": True, "title": row.get("title") or ""}

    def catalog_restore_friend(self, request=None) -> dict:
        """Забрать файл у друга по ЧУЖОЙ строке (этап 2.1, §6(б)).

        Строка в индекс НЕ заводится (правило B3 остаётся): файл
        ложится с пофайловым провенансом origin=friend; хеш - из
        подписанного манифеста автора каталога, sha256 сверяется.
        """
        request = request or {}
        remote_id = str(request.get("remote_id") or "")
        origin = str(request.get("origin") or "")
        addr = str(request.get("addr") or "")
        title = str(request.get("title") or remote_id)
        if not remote_id or not origin or not addr:
            return {"error": "Не указаны строка, устройство и адрес"}
        if repo.catalog_own(self.db.conn, remote_id):
            return {"error": "Строка есть в вашем индексе - "
                             "пользуйтесь обычным восстановлением"}
        config = dict(self._current_settings())
        if not config.get("mesh_enabled"):
            return {"error": "Участие в mesh выключено в настройках "
                             "(секция Mesh)"}
        with self._lock:
            if self._mesh.get("running"):
                return {"error": "Запрос к участникам уже идёт"}
        dest = str(config.get("dest_dir") or profile_dir())
        self._mesh_stop.clear()
        with self._lock:
            self._mesh = {"running": True, "video_id": None, "title": title,
                          "error": None, "summary": None}

        def work() -> None:
            try:
                ok = mesh_fetch.restore_friend(
                    self.db.conn,
                    {"remote_id": remote_id, "origin": origin,
                     "title": title}, addr, dest,
                    log=self._log, stop=self._mesh_stop)
                summary = ("Файл забран у друга (пометка origin=friend)"
                           if ok else "Забор у друга не удался")
                with self._lock:
                    self._mesh.update(running=False, summary=summary,
                                      error=None if ok else summary)
            except Exception as exc:  # noqa: BLE001 - воркер не валит
                self._log(f"Mesh: забор у друга упал: {exc}")
                with self._lock:
                    self._mesh.update(running=False, error=str(exc),
                                      summary=f"Ошибка: {exc}")

        thread = threading.Thread(target=work, name="omnistash-friend-fetch",
                                  daemon=True)
        with self._lock:
            self._mesh_thread = thread
        thread.start()
        self._log(f"Mesh: забор чужой строки у друга {addr[:24]}… "
                  f"«{title}» (origin=friend, назначение: {dest})")
        return {"ok": True, "title": title}

    def catalog_forget(self, request=None) -> dict:
        """Забыть зеркало участника (строки уходят, устройства не трогаются)."""
        request = request or {}
        try:
            removed = repo.catalog_clear(self.db.conn,
                                         str(request.get("origin") or ""))
        except ValueError as exc:
            return {"error": str(exc)}
        return {"ok": True, "removed": removed}

    # ------------------------------------------------------------------ #
    #  B1: разовая передача по коду (magic-wormhole в отдельном venv)
    # ------------------------------------------------------------------ #

    def wormhole_status(self, request=None) -> dict:
        """Установлен ли wormhole; busy - идёт установка/передача."""
        return {**wormhole_mod.status(), "busy": self._wormhole_busy}

    def _wormhole_run(self, name: str, work) -> dict:
        """Запустить wormhole-действие фоном (одно за раз)."""
        if self._wormhole_busy:
            return {"error": "Передача или установка уже идёт"}
        if not wormhole_mod.installed():
            return {"error": "wormhole не установлен - кнопка "
                             "«Установить» в разделе разовой передачи"}
        self._wormhole_busy = True

        def runner():
            try:
                work()
            finally:
                self._wormhole_busy = False

        threading.Thread(target=runner, name=f"omnistash-{name}",
                         daemon=True).start()
        return {"ok": True, "started": True}

    def wormhole_setup(self, request=None) -> dict:
        """Поставить wormhole в изолированный venv (фоном, один раз)."""
        if self._wormhole_busy:
            return {"error": "Передача или установка уже идёт"}
        if wormhole_mod.installed():
            return {"ok": True, "installed": True}
        self._wormhole_busy = True

        def runner():
            try:
                wormhole_mod.setup(log=self._log)
            finally:
                self._wormhole_busy = False

        threading.Thread(target=runner, name="omnistash-wormhole-setup",
                         daemon=True).start()
        return {"ok": True, "started": True}

    def wormhole_send(self, request=None) -> dict:
        """Отправить файл по коду: код и ход печатаются в журнал."""
        request = request or {}
        path = str(request.get("path") or "").strip()
        if not path:
            return {"error": "Укажите путь к файлу"}
        # LEG-3: первая передача файлу другому лицу - предупреждение
        # о правах (одноразово; фиксируется человеком через legal_accept).
        if not legal_mod.state()["first_send_accepted"]:
            return {"need_confirm": "Вы передаёте файл ДРУГОМУ ЛИЦУ - "
                    "убедитесь, что имеете права на этот файл. "
                    "Продолжить?"}

        def work():
            def on_code(code: str) -> None:
                self._log(f"wormhole: КОД ДЛЯ СОБЕСЕДНИКА: {code}")
            wormhole_mod.send(path, log=self._log, on_code=on_code)

        return self._wormhole_run("wormhole-send", work)

    def wormhole_receive(self, request=None) -> dict:
        """Принять файл по коду в папку загрузок (или указанную)."""
        request = request or {}
        code = str(request.get("code") or "").strip()
        if not code:
            return {"error": "Код не указан"}
        dest = str(request.get("dest") or "").strip() \
            or str(self._current_settings().get("dest_dir")
                   or profile_dir())
        return self._wormhole_run(
            "wormhole-receive",
            lambda: wormhole_mod.receive(code, dest, log=self._log))

    def _mesh_worker(self, row: dict, config: dict, dest_dir: str) -> None:
        try:
            ok = mesh_fetch.restore(self.db.conn, row, config, dest_dir,
                                    log=self._log, stop=self._mesh_stop,
                                    reason="запрос из карточки")
        except Exception as exc:  # noqa: BLE001 - воркер не должен молча умереть
            self._log(f"Mesh: запрос упал: {exc}")
            with self._lock:
                self._mesh.update(running=False, error=str(exc),
                                  summary=f"Ошибка: {exc}")
            return
        if self._mesh_stop.is_set():
            summary = "Остановлено"
        elif ok:
            summary = "Файл получен от участников"
        else:
            summary = "Не удалось получить у участников (см. журнал)"
        with self._lock:
            self._mesh.update(running=False, summary=summary,
                              error=None if ok else summary)
            if ok:
                self._heavy_at = 0.0

    # ------------------------------------------------------------------ #
    #  ffmpeg: докачка по согласию (GPL не вшиваем - см. ffmpeg_installer)
    # ------------------------------------------------------------------ #

    def ffmpeg_start(self, request=None) -> dict:
        """Скачать ffmpeg. Только по явному нажатию пользователя."""
        found = downloader_mod.find_ffmpeg()
        if found:
            # Уже есть (наш, из PATH или у Synfronia) - качать незачем.
            with self._lock:
                self._ffmpeg.update(running=False, phase="found", error=None)
            return {"ok": True, "already": True, "path": found}
        with self._lock:
            if self._ffmpeg.get("running"):
                return {"error": "Установка ffmpeg уже идёт"}
            self._ffmpeg.update(running=True, phase="start", pct=0, error=None)
            self._ffmpeg_stop.clear()
            self._busy = True
            self._status = "Скачиваем ffmpeg…"
        thread = threading.Thread(target=self._ffmpeg_worker, daemon=True,
                                  name="omnistash-ffmpeg")
        self._ffmpeg_thread = thread
        thread.start()
        return {"ok": True}

    def ffmpeg_stop(self) -> dict:
        """Отменить идущую установку (частичные файлы убирает воркер)."""
        with self._lock:
            running = bool(self._ffmpeg.get("running"))
        if not running:
            return {"ok": False}
        self._ffmpeg_stop.set()
        self._log("Остановка установки ffmpeg запрошена")
        return {"ok": True}

    def _ffmpeg_worker(self) -> None:
        def progress(phase: str, pct: float) -> None:
            with self._lock:
                self._ffmpeg.update(phase=phase, pct=int(pct))

        try:
            path = ffmpeg_mod.install(progress=progress, log=self._log,
                                      stop=self._ffmpeg_stop)
        except ffmpeg_mod.InstallCancelled:
            with self._lock:
                self._ffmpeg.update(running=False, phase="cancelled", pct=0)
                self._busy = False
                self._status = "Готово"
            self._log("Установка ffmpeg отменена")
        except Exception as exc:  # noqa: BLE001 - фон не должен молча умереть
            with self._lock:
                self._ffmpeg.update(running=False, phase="error",
                                    error=str(exc))
                self._busy = False
                self._status = "Готово"
            self._log(f"ffmpeg не установлен: {exc}")
        else:
            with self._lock:
                self._ffmpeg.update(running=False, phase="done", pct=100,
                                    error=None)
                self._busy = False
                self._status = "Готово"
            self._log(f"ffmpeg готов: {path}")

    # ------------------------------------------------------------------ #
    #  Аккаунты Google: копии кук, привязанные к источникам (google_auth)
    # ------------------------------------------------------------------ #

    LOGIN_TIMEOUT = 15 * 60     # секунд жизни окна входа

    # Опознаватели сессии Google: без них снимок «вошли» не считаем -
    # страница выдаёт куки и до входа (рекламные, визитёрские).
    _LOGIN_MARKERS = ("SID", "HSID", "SSID", "SAPISID", "LOGIN_INFO",
                      "__Secure-1PSID", "__Secure-3PSID")

    def account_login_start(self, request=None) -> dict:
        """Открыть окно входа; после входа куки становятся НОВЫМ аккаунтом.

        Окно живёт само: как только площадка выдаст сессию (аккаунтские
        куки на google/youtube), создаётся аккаунт, окно закрывается.
        """
        with self._lock:
            if self._login_window is not None:
                return {"error": "Окно входа уже открыто"}
        try:
            import webview
        except Exception as exc:  # noqa: BLE001
            return {"error": f"pywebview недоступен: {exc}"}
        try:
            window = webview.create_window(
                "Войдите в Google - Omnistash",
                "https://www.youtube.com/signin",
                width=540, height=780, min_size=(480, 620))
        except Exception as exc:  # noqa: BLE001
            return {"error": f"Не удалось открыть окно: {exc}"}
        if window is None:
            return {"error": "Окно не создалось - повторите"}
        self._login_cancel.clear()
        with self._lock:
            self._login_window = window
            self._login_facts = {}
            self._account_note = ("Идёт вход: войдите в аккаунт в открытом "
                                  "окне")
        thread = threading.Thread(target=self._login_worker, args=(window,),
                                  daemon=True, name="omnistash-glogin")
        self._login_thread = thread
        thread.start()
        self._log("Окно входа открыто: куки снимутся автоматически, "
                  "окно закроется само")
        return {"ok": True}

    def account_capture_now(self, request=None) -> dict:
        """«Я вошёл — забрать куки»: разовая поимка по кнопке.

        Авто-поимка сама закрывает окно, но если окно закрыли руками или
        воркер пропустил момент - куки снимаются здесь, с понятным итогом:
        либо аккаунт, либо «увидел кук N, аккаунтских M».
        """
        with self._lock:
            window = self._login_window
        if not window:
            return {"error": "Окно входа не открыто"}
        try:
            cookies, kind = self._window_cookies(window)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"Окно не отдало куки: {exc}"}
        facts = google_auth.cookies_facts(cookies)
        markers = sum(1 for c in cookies
                      if c.name in self._LOGIN_MARKERS)
        facts["markers"] = markers
        facts["format"] = kind
        with self._lock:
            self._login_facts = dict(facts)
        self._log("Ручная поимка: увидено кук {} (google {}, формат {}), "
                  "аккаунтских {}, домены: {}".format(
                      facts["total"], facts["google"], kind, facts["markers"],
                      ", ".join(facts["domains"]) or "нет"))
        google = [c for c in cookies if google_auth.is_google_cookie(c)]
        if len(google) < 3 or not markers:
            return {"error":
                    f"Сессии нет: увидено кук {facts['total']} "
                    f"(Google/YouTube: {facts['google']}), аккаунтских "
                    f"{facts['markers']}. Дождитесь полного входа на "
                    "странице YouTube и повторите."}
        try:
            account = google_auth.create_account(
                google, label=self._guess_account_label(window)
                or "вход выполнен (окно)")
        except (OSError, RuntimeError) as exc:
            return {"error": f"Не удалось сохранить аккаунт: {exc}"}
        with self._lock:
            self._account_note = (f"Аккаунт добавлен: {account['label']} - "
                                  "привяжите его к источнику")
        self._log(f"Аккаунт Google добавлен (ручная поимка): "
                  f"{account['label']} (id {account['id']})")
        return {"ok": True, "account": account}

    def account_login_stop(self) -> dict:
        """Закрыть окно входа вручную (аккаунт не создаётся)."""
        with self._lock:
            window = self._login_window
        if not window:
            return {"ok": False}
        self._login_cancel.set()
        try:
            window.destroy()
        except Exception:  # noqa: BLE001 - окно могло закрыться само
            pass
        return {"ok": True}

    def account_visible(self, request=None) -> dict:
        """Диагностика поимки: что окно входа ВИДИТ сейчас (без значений).

        Если поимка не срабатывает, эта кнопка сразу говорит почему: кук
        нет вовсе (окно/страница не отдаёт) или они есть, но не аккаунтские.
        """
        with self._lock:
            window = self._login_window
        if not window:
            return {"error": "Окно входа не открыто"}
        try:
            cookies, kind = self._window_cookies(window)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"Окно не отдало куки: {exc}"}
        facts = google_auth.cookies_facts(cookies)
        markers = sum(1 for c in cookies
                      if c.name in self._LOGIN_MARKERS)
        facts["markers"] = markers
        facts["format"] = kind
        with self._lock:
            self._login_facts = dict(facts)
        return facts

    def _guess_account_label(self, window) -> str:
        """Email со страницы входа - если страница его показывает."""
        try:
            found = window.evaluate_js(
                "(() => { const m = document.body.innerText.match("
                "/[\\w.+-]+@[\\w.-]+\\.[A-Za-z]{2,}/); "
                "return m ? m[0] : ''; })()")
            return str(found or "").strip()[:120]
        except Exception:  # noqa: BLE001 - без email тоже проживём
            return ""

    def _window_host(self, window) -> str:
        """Хост текущей страницы окна - домен по умолчанию для morsel-кук."""
        try:
            from urllib.parse import urlparse
            return (urlparse(window.get_current_url() or "").hostname or "")
        except Exception:  # noqa: BLE001 - окно могло закрыться
            return ""

    def _window_cookies(self, window) -> tuple:
        """Куки окна в нормализованном виде: (список Cookie, формат).

        pywebview отдаёт и Cookie, и list[SimpleCookie] - формат пишем в
        журнал, чтобы смена поведения библиотеки была видна сразу.
        """
        raw = window.get_cookies() or []
        if isinstance(raw, (list, tuple)) and raw:
            kind = type(raw[0]).__name__
        else:
            kind = type(raw).__name__
        return (google_auth.as_cookie_list(
            raw, default_domain=self._window_host(window)), kind)

    def _login_worker(self, window) -> None:
        """Опрос окна до поимки сессии -> новый аккаунт в реестре."""
        deadline = time.time() + self.LOGIN_TIMEOUT
        picked: list = []
        label = ""
        last_facts: dict = {}
        closed_by_user = threading.Event()

        def on_closed(*_args):
            # Пользователь закрыл окно сам: воркер обязан заметить сразу,
            # а не ждать таймаута - иначе «Войти» заблокирован минутами.
            closed_by_user.set()

        try:
            window.events.closed += on_closed
        except Exception:  # noqa: BLE001 - подписка возможна не везде
            on_closed = None
        try:
            while time.time() < deadline:
                if self._login_cancel.is_set() or closed_by_user.is_set():
                    break
                try:
                    cookies, kind = self._window_cookies(window)
                except Exception:  # noqa: BLE001 - окно могли закрыть
                    if closed_by_user.is_set():
                        break       # окно мертво - не крутим до таймаута
                    cookies, kind = [], "?"
                facts = google_auth.cookies_facts(cookies)
                facts["markers"] = sum(1 for c in cookies
                                       if c.name in self._LOGIN_MARKERS)
                with self._lock:
                    self._login_facts = dict(facts)
                # Диагностика в журнал - при КАЖДОМ изменении картины, без
                # значений кук: если поимка не работает, причина читается
                # по журналу сразу, а не через полчаса.
                if facts != last_facts:
                    last_facts = dict(facts)
                    self._log("Вход в Google: увидено кук {} (google {}, "
                              "формат {}), аккаунтских {}, домены: {}".format(
                                  facts["total"], facts["google"], kind,
                                  facts["markers"],
                                  ", ".join(facts["domains"]) or "нет"))
                google = [c for c in cookies
                          if google_auth.is_google_cookie(c)]
                if (len(google) >= 3 and
                        any(c.name in self._LOGIN_MARKERS for c in google)):
                    picked = google
                    label = self._guess_account_label(window)
                    break
                time.sleep(2)
        finally:
            if on_closed is not None:
                try:
                    # Отписка ДО destroy: иначе наш же destroy выставил бы
                    # флаг и статус соврал бы «закрыл пользователь».
                    window.events.closed -= on_closed
                except Exception:  # noqa: BLE001
                    pass
            try:
                window.destroy()
            except Exception:  # noqa: BLE001
                pass
        with self._lock:
            self._login_window = None
        if not picked:
            facts = dict(self._login_facts)
            with self._lock:
                if self._login_cancel.is_set():
                    # Прерывание нашей кнопкой (destroy тоже шлёт closed,
                    # поэтому приоритет у флага остановки).
                    self._account_note = ("Вход прерван по кнопке - можно "
                                          "открыть заново")
                elif closed_by_user.is_set():
                    self._account_note = ("Вход прерван: окно закрыли до "
                                          "поимки куки - можно открыть заново")
                else:
                    self._account_note = (
                        "Вход не завершён: куки не получены (площадка не "
                        "пустила). Диагностика: увидено кук "
                        f"{facts.get('total', 0)}, аккаунтских "
                        f"{facts.get('markers', 0)}")
            self._log("Вход в Google: куки не получены" + (
                " (прервано по кнопке)" if self._login_cancel.is_set()
                else " (окно закрыли до поимки)" if closed_by_user.is_set()
                else ""))
            return
        try:
            account = google_auth.create_account(
                picked, label=label or "вход выполнен (окно)")
        except (OSError, RuntimeError) as exc:
            with self._lock:
                self._account_note = f"Не удалось сохранить аккаунт: {exc}"
            self._log(f"Вход в Google: аккаунт не сохранён - {exc}")
            return
        settings.set_value("google_account_label", "")   # легаси-ключи чистим
        settings.set_value("google_account_since", "")
        with self._lock:
            self._account_note = (f"Аккаунт добавлен: {account['label']} - "
                                  "привяжите его к источнику")
            self._heavy_at = 0.0
        self._log(f"Аккаунт Google добавлен: {account['label']} "
                  f"(id {account['id']})")

    def account_import(self, request=None) -> dict:
        """Импорт cookies.txt (Netscape) -> новый аккаунт."""
        try:
            import webview
        except Exception as exc:  # noqa: BLE001
            return {"error": f"pywebview недоступен: {exc}"}
        win = self._window
        if win is None:
            win = webview.windows[0] if webview.windows else None
        if win is None:
            return {"error": "Окно ещё не готово - повторите через секунду"}
        try:
            dialog_type = getattr(webview, "FileDialog", None)
            kind = (dialog_type.OPEN if dialog_type is not None
                    else getattr(webview, "OPEN_DIALOG", 0))
            chosen = win.create_file_dialog(
                kind, allow_multiple=False,
                file_types=("Файлы кук (*.txt)", "*.txt"))
        except Exception as exc:  # noqa: BLE001
            return {"error": f"Диалог не открылся: {exc}"}
        if not chosen:
            return {"cancelled": True}
        path = chosen[0] if isinstance(chosen, (list, tuple)) else chosen
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
            account = google_auth.create_account_from_text(text)
        except (OSError, RuntimeError, ValueError) as exc:
            with self._lock:
                self._account_note = f"Импорт не удался: {exc}"
            self._log(f"Импорт cookies.txt: {exc}")
            return {"error": str(exc)}
        settings.set_value("google_account_label", "")
        settings.set_value("google_account_since", "")
        with self._lock:
            self._account_note = (f"Аккаунт добавлен: {account['label']} - "
                                  "привяжите его к источнику")
        self._log(f"Аккаунт Google добавлен из файла: {account['label']} "
                  f"(id {account['id']})")
        return {"ok": True, "account": account}

    def account_forget(self, request=None) -> dict:
        """Забыть аккаунт: удалить копию кук и запись реестра."""
        account_id = str((request or {}).get("id") or "")
        if not account_id:
            return {"error": "Не указан аккаунт"}
        result = google_auth.remove_account(account_id)
        with self._lock:
            self._account_note = ("Аккаунт забыт" if result.get("removed")
                                  else "Аккаунт забыт (копии и так не было)")
        self._log(f"Аккаунт Google забыт: {account_id}")
        return {"ok": True, "removed": bool(result.get("removed"))}

    # ------------------------------------------------------------------ #
    #  Расписание
    # ------------------------------------------------------------------ #

    def _scheduled_scan(self) -> bool:
        """Задача таймера: переиндексация. False = «занято, повтор позже»."""
        if self._busy:
            return False
        return not (self.start_scan() or {}).get("error")

    def _scheduled_sync(self) -> bool:
        """Задача таймера: синхронизация источников."""
        if self._busy:
            return False
        return not (self.sync_start() or {}).get("error")

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

    def sync_start(self, request=None) -> dict:
        """Актуализация: снапшот источников -> diff -> запись, а по
        умолчанию сразу и второй этап - метаданные видео вне источников.

        request:
          source_id - синк одного источника (без второго этапа);
          meta_after - выполнять ли второй этап (по умолчанию True:
            любая «синхронизация всё» - полная актуализация; так же
            ведут себя таймер расписания и --sync под планировщик).
        """
        request = request or {}
        source_id = request.get("source_id")
        meta_after = bool(request.get("meta_after", True)) and not source_id
        with self._lock:
            if self._sync.get("running"):
                return {"error": "Синхронизация уже идёт"}
            if self._refresh.get("running"):
                return {"error": "Идёт обновление метаданных - дождитесь"}
            if self._add.get("phase") in ("fetching", "committing"):
                return {"error": "Сначала закончите добавление источника"}
            if self._scan and self._scan.get("running"):
                return {"error": "Дождитесь переиндексации"}
            sources_list = [s for s in repo.sources(self.db.conn)
                            if s.get("url") and s.get("kind") in
                            ("uploads", "remote", "mix")]
            if source_id:
                try:
                    wanted = int(source_id)
                except (TypeError, ValueError):
                    wanted = -1
                sources_list = [s for s in sources_list
                                if s.get("id") == wanted]
            if not sources_list:
                return {"error": "Источник не найден" if source_id
                        else "Нет источников для синхронизации"}
            config = dict(self._current_settings())
            self._sync_stop.clear()
            self._refresh_stop.clear()
            self._sync = {"running": True, "index": 0,
                          "total": len(sources_list), "current": None,
                          "fetch": None, "stage": None, "results": [],
                          "new_ids": [], "new_total": 0, "queued": 0,
                          "error": None,
                          # Этап 2 «метаданные»: виден на этой же вкладке.
                          "meta_active": False, "meta": None}
            self._busy = True
            self._status = f"Синхронизация 0/{len(sources_list)}"
        self._log(f"Синхронизация: источников {len(sources_list)}"
                  + ("" if meta_after else " (только наполнение)"))
        thread = threading.Thread(target=self._sync_worker,
                                  args=(sources_list, config, meta_after),
                                  name="omnistash-sync", daemon=True)
        self._sync_thread = thread
        thread.start()
        return {"ok": True, "sources": len(sources_list),
                "meta_after": meta_after}

    def sync_stop(self) -> dict:
        """Остановить между источниками, между чанками и на этапе 2
        (БД не пострадает): гасим и второй этап, если он уже идёт."""
        if self._sync.get("running"):
            self._sync_stop.set()
            if self._sync.get("meta_active"):
                self._refresh_stop.set()
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

    def _sync_worker(self, sources_list: list[dict], config: dict,
                     meta_after: bool = False) -> None:
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
                        # Куки аккаунта источника: «не бот» проходится
                        # от имени привязанной учётки.
                        account_id=source.get("account_id"),
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
                    # «Полная»: в очередь встаёт выбранное источником
                    # хранилище (playlists.storage_id), а не глобальное -
                    # выбор делал пользователь при добавлении.
                    entry["queued"] = repo.enqueue_playlist(
                        conn, stats["playlist_id"],
                        storage_id=source.get("storage_id"))
                    queued_total += entry["queued"]

                with self._lock:
                    self._sync["results"].append(entry)
                    self._sync["new_total"] = len(new_ids)
                    self._heavy_at = 0.0
                if stats["new_videos"] or stats.get("removed"):
                    self._log(f"{title}: новых {stats['new_videos']}, "
                              f"убрано {stats.get('removed') or 0}")

            # Этап 2 «метаданные»: записи источников только что освежил
            # первый этап - дальше видео ВНЕ источников (единичные), чтобы
            # не дублировать сотни запросов по уже свежим записам.
            if meta_after and not stop.is_set():
                self._sync_meta_stage(conn)
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
            if queued_total and not stopped:
                # «Полная» обязана доехать до качалки сама - и в окне, и в
                # --sync под планировщик, иначе режим ничего не качает.
                self._log(f"Очередь после синка: {queued_total}, запускаю")
                self.dl.start()

    def _sync_meta_stage(self, conn) -> None:
        """Этап 2 актуализации: метаданные видео вне источников.

        Идёт в том же потоке (sync.running остаётся True - этап виден на
        вкладке), но со своим состоянием refresh (кнопка «Остановить» в
        списке библиотеки тоже гасит его).
        """
        if self._sync_stop.is_set():
            return
        rows = [dict(r) for r in conn.execute(
            """SELECT id, remote_id, webpage_url FROM videos
                WHERE platform <> 'local'
                  AND id NOT IN (SELECT video_id FROM playlist_items)
                ORDER BY id""")]
        if not rows:
            return
        with self._lock:
            if self._sync_stop.is_set():
                return          # остановили на границе этапов
            self._refresh = {"running": True, "done": 0, "total": len(rows),
                             "current": "", "errors": 0, "summary": None}
            self._sync["meta_active"] = True
            self._sync["meta"] = {"done": 0, "total": len(rows),
                                  "current": "", "errors": 0}
        self._log(f"Актуализация: этап 2 - метаданные {len(rows)} видео "
                  "вне источников")
        try:
            self._refresh_worker(rows, final=False)
        finally:
            with self._lock:
                self._sync["meta_active"] = False

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
        self._repack_stop.set()
        self._verify_stop.set()
        self._ffmpeg_stop.set()
        self._mesh_stop.set()
        self._login_cancel.set()
        with self._lock:
            login_window = self._login_window
        if login_window is not None:
            try:
                login_window.destroy()
            except Exception:  # noqa: BLE001 - окно могло закрыться само
                pass
        self.dl.stop()
        self.sched.stop()
        for thread in (self._scan_thread, self._add_thread, self._sync_thread,
                       self._migrate_thread, self._repack_thread,
                       self._verify_thread, self._ffmpeg_thread,
                       self._mesh_thread,
                       getattr(self, "_avail_thread", None),
                       getattr(self, "_check_thread", None)):
            if thread is not None and thread.is_alive():
                thread.join(timeout=15)
        self._mesh_server.stop()
        self._mesh_dht.stop_sync()
        mesh_dht.set_current(None)
        self._nat_stop()
        self.dl.wait(timeout=15)
        self.db.close()


def headless_sync() -> int:
    """`python omnistash.py --sync`: синхронизация и очередь, без окна.

    Заточен под планировщик Windows: отработал, вышел, код возврата что-то
    говорит о результате. Журнал при этом пишется в omnistash.log, поэтому
    после запуска видно, что произошло.

      0 - синк отработал (в том числе «нечего качать»);
      1 - запуск не удался: нет источников или ошибка старта;
      2 - синк отработал, но у части источников были ошибки.
    """
    api = Api()
    try:
        started = api.sync_start()
        if started.get("error"):
            print(started["error"])
            return 1
        thread = api._sync_thread
        while thread is not None and thread.is_alive():
            time.sleep(0.5)

        results = list(api._sync.get("results") or [])
        errors = [row for row in results if row.get("error")]
        new_total = sum(int(row.get("new") or 0) for row in results)
        queued = int(api._sync.get("queued") or 0)

        downloaded = 0
        pending = repo.stats(api.db.conn)["queued"]
        if pending:
            # Режим «Полная» сам поставил строки в очередь - доедем их,
            # иначе задача планировщика ограничилась бы метаданными.
            api._log(f"--sync: качаю {pending} строк")
            api.dl.start()
            deadline = time.time() + 3600
            while api.dl.state.get("running") and time.time() < deadline:
                time.sleep(1.0)
            downloaded = int(api.dl.state.get("done") or 0)

        print(f"источников: {len(results)}, новых видео: {new_total}, "
              f"в очередь: {queued}, ошибок источников: {len(errors)}, "
              f"скачано: {downloaded}")
        for row in errors:
            print(f"  ! {row.get('title')}: {row.get('error')}")
        return 2 if errors else 0
    finally:
        api.close()


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
