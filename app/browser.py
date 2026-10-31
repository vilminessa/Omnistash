"""Браузер площадки: второе нативное webview-окно рядом с главным окном.

Честная рамка этапа D: страницы площадок отдают X-Frame-Options/CSP, поэтому
встроенный <iframe> их страницу не загрузит. Вместо него - отдельное нативное
окно (pywebview 6.2 умеет несколько окон), а в вкладке площадки лежит панель:
она показывает захваченный адрес и кнопки «Скачать / Как источник». Внешне это
выглядит как встроенный режим площадки, но окно не врёт: адрес берётся у самой
страницы, а не выдумывается.

Захват: location.href через evaluate_js. Окно, которое не ответило на запрос
(или не ответило за отведённое время), - это «адрес не получен»; пустого
адреса мы не показываем как «адрес есть».

Фоллбэк: если webview-бкенд на этой машине не поднимается (нет бкенда или
браузера, окно не создалось), ссылка открывается в системном браузере
(subprocess - как проводник в gui.py), и человек сам вставляет адрес из
браузера. Никаких traceback' наружу: исключение бкенда превращается в
понятную строку ответа, окно приложения не роняется.

Площадка и её домены берутся из реестра (app.platforms); юридические гейты
здесь не решаются - их проверяет окно (gui.Api), как и в add_start.
"""

from __future__ import annotations

import os
import subprocess
import threading
from urllib.parse import urlparse

from . import platforms as platforms_mod
from .util import clean_text, now_iso

MAX_WINDOWS = 4          # сколько окон площадки держим одновременно
WINDOW_TITLE = "Площадка - Omnistash"   # нейтрально: имя площадки не пишем
WINDOW_WIDTH = 1000
WINDOW_HEIGHT = 760
WINDOW_MIN_SIZE = (720, 540)
EVALUATE_TIMEOUT = 5     # секунд: кривая страница не должна вешать API окна
MAX_URL_LEN = 400        # ссылка длиннее - это мусор, а не адрес площадки

# Адрес страницы: только то, что страница сама про себя знает. try внутри
# JS, чтобы evaluate_js не бросал JavascriptException на кривой странице.
ADDRESS_JS = "(() => { try { return location.href; } catch (e) { return ''; } })()"

# Честная формулировка отказа: её видит человек (UI показывает вместо адреса).
NOT_RECEIVED = "адрес не получен"


def home_url(platform_id) -> str | None:
    """Адрес площадки для «открыть окно»: первый домен из реестра площадок.

    Реестр (app.platforms) - единственный источник доменов: здесь ничего не
    придумываем. Для неизвестной площадки - None (окно скажет честно).
    """
    item = platforms_mod.by_id(platform_id)
    if not item:
        return None
    domains = tuple(item.get("domains") or ())
    if not domains:
        return None
    return "https://www." + str(domains[0])


def clean_url(value) -> str:
    """Ссылка для окна: только http/https, без мусора и ANSI-кодов.

    Не http/https -> "" (открывать нечего, в том числе javascript: и file:).
    """
    text = clean_text(value)[:MAX_URL_LEN].strip()
    if not text.startswith(("http://", "https://")):
        return ""
    if not urlparse(text).netloc:
        return ""
    return text


def _short(value, limit: int = 100) -> str:
    """Короткая строка в сообщение: длинный адрес или текст исключения."""
    text = clean_text(value)
    return text if len(text) <= limit else text[:limit] + "…"


def open_in_system_browser(url: str) -> dict:
    """Открыть ссылку в системном браузере (фоллбэк без webview-бкенда).

    Windows: тот же путь через проводник, что уже используется в gui.py для
    папки; другие платформы - штатный webbrowser. Ничего не роняем: не
    открылось - понятная строка.
    """
    text = clean_url(url)
    if not text:
        return {"error": "Ссылка не http/https: открывать нечего"}
    if os.name == "nt":
        try:
            subprocess.Popen(["explorer", text])
        except OSError as exc:
            return {"error": f"Системный браузер не открылся: {clean_text(exc)}"}
        return {"ok": True}
    try:
        import webbrowser
        if not webbrowser.open(text):
            return {"error": "Системный браузер не ответил"}
    except Exception as exc:  # noqa: BLE001 - браузер может быть не настроен
        return {"error": f"Системный браузер не открылся: {clean_text(exc)}"}
    return {"ok": True}


def _evaluate(window) -> tuple[str, str]:
    """location.href из окна, с ограничением по времени.

    evaluate_js у pywebview блокирует до ответа страницы: поток API не должен
    висеть бесконечно, поэтому ждём своё и честно пишем, если ответа нет.
    """
    result: dict = {}

    def worker() -> None:
        try:
            result["value"] = window.evaluate_js(ADDRESS_JS)
        except Exception as exc:  # noqa: BLE001 - окно отвечает по-разному
            result["error"] = clean_text(exc) or "окно не ответило"

    thread = threading.Thread(target=worker, daemon=True,
                              name="omnistash-browser-address")
    thread.start()
    thread.join(EVALUATE_TIMEOUT)
    if thread.is_alive():
        return "", f"{NOT_RECEIVED}: окно не ответило за {EVALUATE_TIMEOUT} секунд"
    if result.get("error"):
        return "", f"{NOT_RECEIVED}: окно не ответило на запрос ({_short(result['error'])})"
    text = str(result.get("value") or "").strip()
    if not text:
        return "", f"{NOT_RECEIVED}: страница ещё не загружена"
    if text == "about:blank":
        return "", f"{NOT_RECEIVED}: страница ещё не загружена (about:blank)"
    if not text.startswith(("http://", "https://")):
        return "", f"{NOT_RECEIVED}: страница вернула не адрес ({_short(text)})"
    return text, ""


class Browser:
    """Окна площадки: реестр записей и нативные окна рядом с главным.

    Каждый ответ - dict (окно приложения не роняется: исключение бкенда
    превращается в понятную строку). Журнал пишет вызывающий (gui.Api), здесь
    log только для служебных строк, если он передан.
    """

    def __init__(self, log=None) -> None:
        self._log = log or (lambda text: None)
        self._lock = threading.RLock()
        self._records: dict[str, dict] = {}
        self._seq = 0

    # ------------------------------------------------------------------ #
    #  Реестр окон
    # ------------------------------------------------------------------ #

    def windows(self) -> list[dict]:
        """Открытые окна площадки (публичный вид: без объектов pywebview)."""
        with self._lock:
            return [_public(record) for record in self._records.values()]

    def reset(self) -> None:
        """Сброс состояния (тесты: окно живёт в модуле, его надо очистить)."""
        self.close_all()
        with self._lock:
            self._records = {}
            self._seq = 0

    def _record(self, mode: str, url: str, label: str, window) -> dict:
        with self._lock:
            self._seq += 1
            record = {"id": f"w{self._seq}", "url": url, "label": label,
                      "mode": mode, "status": "open", "window": window,
                      "address": "", "note": "", "opened_at": now_iso(),
                      "captured_at": ""}
            self._records[record["id"]] = record
        return record

    def _pick(self, window_id=None) -> dict | None:
        """Запись по id или первая открытая (без id человек не выбирает окно)."""
        with self._lock:
            if window_id:
                return self._records.get(str(window_id))
            for record in self._records.values():
                if record["status"] == "open":
                    return record
            return next(iter(self._records.values()), None)

    # ------------------------------------------------------------------ #
    #  Открытие окна
    # ------------------------------------------------------------------ #

    def open(self, url: str, *, label: str = "") -> dict:
        """Открыть страницу площадки в отдельном нативном окне.

        Бкенд, который не поднялся, - это не ошибка приложения: окно
        открывается в системном браузере (фоллбэк) и ответ говорит, какой
        режим был использован.
        """
        text = clean_url(url)
        if not text:
            return {"error": "Ссылка не http/https: открывать нечего"}
        with self._lock:
            alive = [record for record in self._records.values()
                     if record["status"] == "open"]
            if len(alive) >= MAX_WINDOWS:
                return {"error": f"Открыто уже {MAX_WINDOWS} окон площадки - "
                                 "закройте лишнее"}
        try:
            import webview
        except Exception as exc:  # noqa: BLE001 - машина без pywebview
            return self._external(text, label,
                                  f"pywebview недоступен: {_short(exc)}")
        try:
            window = webview.create_window(
                WINDOW_TITLE, url=text, width=WINDOW_WIDTH, height=WINDOW_HEIGHT,
                min_size=WINDOW_MIN_SIZE)
        except Exception as exc:  # noqa: BLE001 - бкенд может не подняться
            return self._external(text, label,
                                  f"окно не поднялось: {_short(exc)}")
        if window is None:
            # pywebview вернул None, если инициализация окна отменена
            return self._external(text, label, "окно не создалось")
        record = self._record("webview", text, label, window)
        self._watch_closed(record)
        return {"ok": True, "id": record["id"], "mode": "webview",
                "url": text, "label": label, "fallback": False,
                "windows": self.windows()}

    def open_external(self, url: str, *, label: str = "") -> dict:
        """Явный фоллбэк: открыть ссылку в системном браузере человека."""
        text = clean_url(url)
        if not text:
            return {"error": "Ссылка не http/https: открывать нечего"}
        return self._external(text, label, "выбран системный браузер")

    def _external(self, url: str, label: str, reason: str) -> dict:
        """Фоллбэк: системный браузер + просьба вставить адрес из него."""
        opened = open_in_system_browser(url)
        if opened.get("error"):
            self._log(f"Фоллбэк не сработал: {opened['error']} ({reason})")
            return {"error": opened["error"], "reason": reason}
        record = self._record("system", url, label, None)
        record["status"] = "external"
        record["note"] = (f"{NOT_RECEIVED}: {reason} - адрес вставьте из "
                          "браузера в поле")
        self._log(f"Площадка открыта в системном браузере: {url} ({reason})")
        return {"ok": True, "id": record["id"], "mode": "system",
                "url": url, "label": label, "fallback": True,
                "reason": reason, "note": record["note"],
                "windows": self.windows()}

    def _watch_closed(self, record: dict) -> None:
        """Человек закрыл окно сам: запись остаётся с последним адресом.

        Окно без events (мок) - подписка не нужна, статус виден по ответу.
        """
        events = getattr(record["window"], "events", None)
        closed = getattr(events, "closed", None) if events else None
        if closed is None:
            return

        def on_closed(*args) -> None:
            with self._lock:
                if record["status"] != "open":
                    return          # окно закрыла мы сами - это не человек
                record["status"] = "closed"
                record["window"] = None
                record["note"] = f"{NOT_RECEIVED}: окно закрыто"
            self._log(f"Окно площадки закрыто человеком: {record['url']}")

        try:
            closed += on_closed     # pywebview: Event += handler
        except Exception:  # noqa: BLE001 - без подписки окно не роняется
            pass

    # ------------------------------------------------------------------ #
    #  Захват адреса
    # ------------------------------------------------------------------ #

    def address(self, window_id=None) -> dict:
        """Текущий адрес страницы окна: location.href у самой страницы.

        Выдумывать нечего: если окна нет, оно закрыто или бкенд не ответил,
        ответ пишет «адрес не получен» (последний известный адрес остаётся).
        """
        record = self._pick(window_id)
        if record is None:
            return {"error": "Окно площадки не открыто - нажмите "
                             "«Открыть площадку»"}
        if record["mode"] != "webview":
            return {"ok": True, "id": record["id"], "address": "",
                    "note": record["note"] or
                    f"{NOT_RECEIVED}: адрес открыт в системном браузере - "
                    "вставьте его из браузера",
                    "windows": self.windows()}
        if record["status"] != "open":
            return {"ok": True, "id": record["id"],
                    "address": record["address"],
                    "note": record["note"] or f"{NOT_RECEIVED}: окно закрыто",
                    "windows": self.windows()}

        value, note = _evaluate(record["window"])
        with self._lock:
            if value:
                record["address"] = value
                record["note"] = ""
                record["captured_at"] = now_iso()
            else:
                record["note"] = note
            address = record["address"]
            captured = record["captured_at"]
        if value:
            self._log(f"Адрес окна площадки: {value}")
        else:
            self._log(f"Адрес не получен: {note}")
        return {"ok": True, "id": record["id"], "address": address,
                "note": record["note"], "captured_at": captured,
                "windows": self.windows()}

    # ------------------------------------------------------------------ #
    #  Закрытие
    # ------------------------------------------------------------------ #

    def close(self, window_id=None) -> dict:
        """Закрыть окно площадки (или первое открытое)."""
        record = self._pick(window_id)
        if record is None:
            return {"error": "Окно площадки не открыто"}
        window = record["window"]
        if window is None:
            with self._lock:
                record["status"] = "closed"
            self._log(f"Окно площадки уже закрыто: {record['url']}")
            return {"ok": True, "id": record["id"], "windows": self.windows()}
        with self._lock:
            record["status"] = "closed"
            record["window"] = None
        try:
            window.destroy()
        except Exception as exc:  # noqa: BLE001 - окно могло закрыться само
            with self._lock:
                record["status"] = "open"
                record["window"] = window
                record["note"] = f"окно не закрылось: {_short(exc)}"
            self._log(f"Окно площадки не закрылось: {_short(exc)}")
            return {"error": f"Окно не закрылось: {_short(exc)}",
                    "windows": self.windows()}
        self._log(f"Окно площадки закрыто: {record['url']}")
        return {"ok": True, "id": record["id"], "windows": self.windows()}

    def close_all(self) -> dict:
        """Закрыть все окна приложения (вызывается при закрытии окна)."""
        with self._lock:
            records = list(self._records.values())
        closed = 0
        for record in records:
            window = record["window"]
            if window is None:
                continue
            with self._lock:
                record["status"] = "closed"
                record["window"] = None
            try:
                window.destroy()
                closed += 1
            except Exception:  # noqa: BLE001 - окно могло закрыться само
                pass
        return {"ok": True, "closed": closed}


def _public(record: dict) -> dict:
    """Запись без объекта окна: то, что можно показать человеку."""
    return {
        "id": record["id"],
        "url": record["url"],
        "label": record["label"],
        "mode": record["mode"],
        "status": record["status"],
        "open": record["status"] == "open",
        "address": record["address"],
        "note": record["note"],
        "opened_at": record["opened_at"],
        "captured_at": record["captured_at"],
    }


# Окно живёт одно состояние: панель площадки не плодит окна на каждый вызов.
SESSION = Browser()


def open_window(url: str, label: str = "") -> dict:
    return SESSION.open(url, label=label)


def open_system_browser(url: str, label: str = "") -> dict:
    return SESSION.open_external(url, label=label)


def address(window_id=None) -> dict:
    return SESSION.address(window_id)


def close(window_id=None) -> dict:
    return SESSION.close(window_id)


def close_all() -> dict:
    return SESSION.close_all()


def windows() -> list[dict]:
    return SESSION.windows()


def reset() -> None:
    SESSION.reset()
