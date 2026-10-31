"""D: окно площадки и захват адреса (app.browser) — офлайн, без webview и браузера.

Честная рамка этапа: страницы площадок отдают X-Frame-Options/CSP, поэтому
встроенный <iframe> их не грузит — вместо него второе нативное webview-окно,
а панель вкладки показывает адрес, который страница про себя знает.

Проверяем то, что не видно из кода окна:
  * адрес берётся у самой страницы (evaluate_js), а не выдумывается;
  * «адрес не получен» честный: бкенд не ответил, ответил не тем, окно
    закрыто, страница ещё не загружена (about:blank);
  * бкенд, который не поднялся, не роняет приложение — фоллбэк в системный
    браузер, и ответ говорит, какой режим был использован;
  * лимит окон, закрытие (наше и человеком), close_all;
  * гейт 18+ на открытии взрослой площадки и воронка add_start по адресу;
  * публичные тексты панели — нейтральные (имя и домены взрослой площадки
    в тексте окна не звучат).

webview подменяется заглушкой в sys.modules (на этой машине бкенд есть, но
офлайн-прогон не должен поднимать настоящие окна), системный браузер — тоже:
реальный проводник в тесте не запускается.
"""

import sys
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app import browser
from tests.test_gui import GuiCase


class FakeEvent:
    """Как pywebview Event: подписка через +=, вызов — нами."""

    def __init__(self) -> None:
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self) -> None:
        for handler in self.handlers:
            handler()


class FakeWindow:
    """Окно бкенда: адрес по запросу, зависание, отказ, закрытие."""

    def __init__(self, address="", *, error=None, delay=0.0,
                 destroy_error=None) -> None:
        self.address = address
        self.error = error
        self.delay = delay
        self.destroy_error = destroy_error
        self.destroyed = False
        self.requests = []
        self.events = SimpleNamespace(closed=FakeEvent())

    def evaluate_js(self, script):
        # Спрашиваем ровно то, что окно собирается читать: location.href.
        self.requests.append(script)
        assert script == browser.ADDRESS_JS, script
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        return self.address

    def destroy(self) -> None:
        if self.destroy_error:
            raise self.destroy_error
        self.destroyed = True


def stub_webview(window=None, error=None, factory=None,
                 none: bool = False) -> types.ModuleType:
    """Модуль-заглушка вместо настоящего webview (импорт внутри open()).

    factory — окно на каждый вызов (несколько окон в одном тесте), иначе все
    записи ссылались бы на одно окно и проверка «все закрыты» была бы ложной.
    none=True — бкенд возвращает None (инициализация окна отменена).
    """
    module = types.ModuleType("webview")

    def create_window(title, url=None, **kwargs):
        if error:
            raise error
        # Нейтральный заголовок и размеры — имя площадки туда не пишется.
        assert title == browser.WINDOW_TITLE, title
        assert kwargs.get("width") == browser.WINDOW_WIDTH
        assert kwargs.get("height") == browser.WINDOW_HEIGHT
        if none:
            return None
        if factory is not None:
            return factory(url or "")
        return window if window is not None else FakeWindow(address=url or "")

    module.create_window = create_window
    return module


class BrowserCase(unittest.TestCase):
    """Окно живёт в модуле (SESSION) — каждое тестовое состояние чистим."""

    def setUp(self):
        browser.reset()
        self.addCleanup(browser.reset)
        self.logs: list[str] = []
        patcher = mock.patch.object(browser.SESSION, "_log",
                                    side_effect=self.logs.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def stub_backend(self, window=None, error=None, factory=None, none=False):
        patcher = mock.patch.dict(
            sys.modules,
            {"webview": stub_webview(window, error, factory, none)})
        patcher.start()
        self.addCleanup(patcher.stop)
        return patcher

    def fake_system_browser(self, *, ok: bool = True):
        """Реальный проводник в офлайн-прогоне не запускается."""
        def opener(url):
            if ok:
                return {"ok": True}
            return {"error": "Системный браузер не открылся: тест"}
        patcher = mock.patch.object(browser, "open_in_system_browser",
                                    side_effect=opener)
        patcher.start()
        self.addCleanup(patcher.stop)
        return patcher


class TestRegistry(BrowserCase):
    """Домены и подписи — из реестра площадок, ничего не придумывается."""

    def test_home_url_from_registry(self):
        self.assertEqual(browser.home_url("youtube"), "https://www.youtube.com")
        self.assertEqual(browser.home_url("vk"), "https://www.vk.com")
        # Взрослая площадка тоже доступна функцией (гейт решает окно, не здесь)
        self.assertTrue(browser.home_url("pornhub").startswith("https://"))
        self.assertIsNone(browser.home_url("нет-такой-площадки"))

    def test_clean_url_accepts_only_http(self):
        self.assertEqual(browser.clean_url("https://youtu.be/abc"),
                         "https://youtu.be/abc")
        self.assertEqual(browser.clean_url("http://vk.com/video-1-2"),
                         "http://vk.com/video-1-2")
        for bad in ("", "   ", "javascript:alert(1)", "file:///C:/x.mp4",
                    "mailto:a@b.c", "ftp://host/x", "youtube.com/watch?v=1",
                    "\x1b[31mмусор\x1b[0m"):
            self.assertEqual(browser.clean_url(bad), "", bad)
        self.assertEqual(browser.clean_url("https://host"), "https://host")

    def test_clean_url_truncates_garbage(self):
        long = "https://host/" + "x" * (browser.MAX_URL_LEN + 50)
        self.assertLessEqual(len(browser.clean_url(long)), browser.MAX_URL_LEN)


class TestOpenWindow(BrowserCase):
    def test_open_creates_native_window(self):
        window = FakeWindow(address="https://www.youtube.com/watch?v=1")
        self.stub_backend(window)
        got = browser.open_window("https://www.youtube.com/watch?v=1",
                                  label="YouTube")
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["mode"], "webview")
        self.assertFalse(got["fallback"])
        self.assertTrue(got["windows"][0]["open"])
        # Публичный вид записи: объект окна наружу не отдаётся
        self.assertNotIn("window", got["windows"][0])
        self.assertEqual(browser.windows()[0]["label"], "YouTube")

    def test_open_rejects_non_http_url(self):
        self.stub_backend()
        got = browser.open_window("javascript:alert(1)")
        self.assertTrue(got.get("error"), got)
        self.assertEqual(browser.windows(), [], "отказ не создаёт запись")

    def test_window_limit_is_honest(self):
        self.stub_backend(FakeWindow(address="https://www.youtube.com/"))
        for _ in range(browser.MAX_WINDOWS):
            self.assertTrue(browser.open_window("https://www.youtube.com/")[
                "ok"])
        got = browser.open_window("https://www.youtube.com/watch?v=9")
        self.assertTrue(got.get("error"), got)
        self.assertIn(str(browser.MAX_WINDOWS), got["error"])
        self.assertEqual(len(browser.windows()), browser.MAX_WINDOWS)

    def test_backend_that_does_not_start_falls_back_to_system_browser(self):
        """Бкенд не поднялся — это не ошибка приложения: фоллбэк, и ответ
        честно говорит, что адрес надо вставить самому."""
        self.stub_backend(error=RuntimeError("бокенд не поднят"))
        self.fake_system_browser()
        got = browser.open_window("https://www.youtube.com/watch?v=1")
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["mode"], "system")
        self.assertTrue(got["fallback"])
        self.assertIn(browser.NOT_RECEIVED, got["note"])
        self.assertIn("вставьте", got["note"])
        self.assertEqual(browser.windows()[0]["status"], "external")

    def test_window_created_as_none_falls_back(self):
        self.stub_backend(none=True)
        self.fake_system_browser()
        got = browser.open_window("https://www.youtube.com/watch?v=1")
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["mode"], "system")
        self.assertIn("окно не создалось", got["reason"])

    def test_pywebview_missing_falls_back(self):
        # Модуль webview вообще отсутствует на машине
        patcher = mock.patch.dict(sys.modules, {"webview": None})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fake_system_browser()
        got = browser.open_window("https://www.youtube.com/watch?v=1")
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["mode"], "system")
        self.assertIn("pywebview", got["reason"])

    def test_system_browser_failure_is_reported_not_swallowed(self):
        self.stub_backend(error=RuntimeError("бокенд не поднят"))
        self.fake_system_browser(ok=False)
        got = browser.open_window("https://www.youtube.com/watch?v=1")
        self.assertTrue(got.get("error"), got)
        self.assertEqual(browser.windows(), [], "не открылось — нет записи")

    def test_explicit_system_browser_mode(self):
        self.fake_system_browser()
        got = browser.open_system_browser("https://www.vk.com/video-1-2")
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["mode"], "system")
        self.assertEqual(got["fallback"], True)

    def test_system_browser_rejects_bad_url_without_launching(self):
        # Реальный проводник не должен быть запущен на мусор
        with mock.patch.object(browser.subprocess, "Popen",
                               side_effect=AssertionError("не запускать")):
            got = browser.open_in_system_browser("javascript:alert(1)")
        self.assertTrue(got.get("error"), got)

    def test_system_browser_launches_via_explorer_on_windows(self):
        launched: list[list[str]] = []
        with mock.patch.object(browser.subprocess, "Popen",
                               side_effect=lambda cmd, **kw: launched.append(cmd)):
            got = browser.open_in_system_browser("https://www.youtube.com/watch?v=1")
        self.assertTrue(got.get("ok"), got)
        self.assertEqual(launched, [["explorer",
                                    "https://www.youtube.com/watch?v=1"]])

    def test_system_browser_launch_failure_is_honest(self):
        with mock.patch.object(browser.subprocess, "Popen",
                               side_effect=OSError("нет проводника")):
            got = browser.open_in_system_browser("https://www.youtube.com/")
        self.assertTrue(got.get("error"), got)
        self.assertIn("не открылся", got["error"])

    def test_person_closing_window_marks_it_closed(self):
        window = FakeWindow(address="https://www.youtube.com/watch?v=1")
        self.stub_backend(window)
        got = browser.open_window("https://www.youtube.com/watch?v=1")
        browser.address(got["id"])
        window.events.closed.fire()          # человек закрыл окно сам
        record = browser.windows()[0]
        self.assertFalse(record["open"])
        self.assertIn(browser.NOT_RECEIVED, record["note"])
        self.assertEqual(record["address"], "https://www.youtube.com/watch?v=1",
                         "последний известный адрес остаётся видимым")
        self.assertTrue(any("закрыто человеком" in line for line in self.logs))


class TestAddressCapture(BrowserCase):
    def test_address_is_taken_from_the_page_itself(self):
        window = FakeWindow(address="https://www.youtube.com/watch?v=abc")
        self.stub_backend(window)
        got = browser.open_window("https://www.youtube.com/watch?v=abc")
        result = browser.address(got["id"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["address"], "https://www.youtube.com/watch?v=abc")
        self.assertEqual(result["note"], "")
        self.assertTrue(result["captured_at"])
        self.assertEqual(window.requests, [browser.ADDRESS_JS])
        self.assertTrue(any("Адрес окна площадки" in line for line in self.logs))

    def test_silent_backend_is_not_reported_as_address(self):
        window = FakeWindow(error=RuntimeError("JavascriptException"))
        self.stub_backend(window)
        got = browser.open_window("https://www.youtube.com/watch?v=abc")
        result = browser.address(got["id"])
        self.assertEqual(result["address"], "")
        self.assertIn(browser.NOT_RECEIVED, result["note"])

    def test_hanging_page_does_not_block_forever(self):
        """evaluate_js блокирует до ответа страницы: свой таймаут и честная строка."""
        window = FakeWindow(address="https://www.youtube.com/watch?v=abc",
                            delay=0.6)
        self.stub_backend(window)
        patcher = mock.patch.object(browser, "EVALUATE_TIMEOUT", 0.2)
        patcher.start()
        self.addCleanup(patcher.stop)
        got = browser.open_window("https://www.youtube.com/watch?v=abc")
        started = time.time()
        result = browser.address(got["id"])
        self.assertLess(time.time() - started, 1.5,
                        "поток API завис на бесконечный ответ страницы")
        self.assertEqual(result["address"], "")
        self.assertIn(browser.NOT_RECEIVED, result["note"])
        self.assertIn("не ответило за 0.2 секунд", result["note"])

    def test_unloaded_page_is_not_an_address(self):
        for value in ("", "about:blank"):
            browser.reset()
            self.stub_backend(FakeWindow(address=value))
            got = browser.open_window("https://www.youtube.com/watch?v=abc")
            result = browser.address(got["id"])
            self.assertEqual(result["address"], "")
            self.assertIn(browser.NOT_RECEIVED, result["note"])
            self.assertIn("не загружена", result["note"])

    def test_page_returning_not_a_url_is_not_an_address(self):
        self.stub_backend(FakeWindow(address="file:///C:/video.mp4"))
        got = browser.open_window("https://www.youtube.com/watch?v=abc")
        result = browser.address(got["id"])
        self.assertEqual(result["address"], "")
        self.assertIn("не адрес", result["note"])

    def test_failed_capture_keeps_previous_address(self):
        window = FakeWindow(address="https://www.youtube.com/watch?v=one")
        self.stub_backend(window)
        got = browser.open_window("https://www.youtube.com/watch?v=one")
        self.assertEqual(browser.address(got["id"])["address"],
                         "https://www.youtube.com/watch?v=one")
        window.error = RuntimeError("бокенд перестал отвечать")
        window.address = ""
        result = browser.address(got["id"])
        self.assertEqual(result["address"], "https://www.youtube.com/watch?v=one")
        self.assertIn(browser.NOT_RECEIVED, result["note"])

    def test_address_without_window_says_so(self):
        got = browser.address()
        self.assertTrue(got.get("error"), got)
        self.assertIn("не открыто", got["error"])

    def test_address_of_unknown_window_id_says_so(self):
        self.stub_backend(FakeWindow(address="https://www.youtube.com/"))
        browser.open_window("https://www.youtube.com/")
        got = browser.address("w999")
        self.assertTrue(got.get("error"), got)

    def test_system_mode_asks_to_paste_address(self):
        self.fake_system_browser()
        got = browser.open_system_browser("https://www.youtube.com/watch?v=1")
        result = browser.address(got["id"])
        self.assertEqual(result["address"], "")
        self.assertIn(browser.NOT_RECEIVED, result["note"])
        self.assertIn("вставьте", result["note"])


class TestClose(BrowserCase):
    def test_close_destroys_window_and_keeps_last_address(self):
        window = FakeWindow(address="https://www.youtube.com/watch?v=one")
        self.stub_backend(window)
        got = browser.open_window("https://www.youtube.com/watch?v=one")
        browser.address(got["id"])
        closed = browser.close(got["id"])
        self.assertTrue(closed["ok"], closed)
        self.assertTrue(window.destroyed)
        record = browser.windows()[0]
        self.assertFalse(record["open"])
        self.assertEqual(record["address"], "https://www.youtube.com/watch?v=one")

    def test_close_failure_leaves_window_open(self):
        window = FakeWindow(address="https://www.youtube.com/",
                            destroy_error=RuntimeError(" бкенд отказал"))
        self.stub_backend(window)
        got = browser.open_window("https://www.youtube.com/")
        result = browser.close(got["id"])
        self.assertTrue(result.get("error"), result)
        self.assertTrue(browser.windows()[0]["open"],
                        "отказ закрытия не должен притворяться «закрыто»")

    def test_close_without_window_says_so(self):
        self.assertTrue(browser.close().get("error"))

    def test_close_all_destroys_every_window(self):
        windows: list[FakeWindow] = []

        def factory(url):
            window = FakeWindow(address=url)
            windows.append(window)
            return window

        self.stub_backend(factory=factory)
        for index in range(3):
            browser.open_window(f"https://youtu.be/v{index}")
        self.assertEqual(len(windows), 3)
        got = browser.close_all()
        self.assertEqual(got["closed"], 3, got)
        self.assertTrue(all(window.destroyed for window in windows))
        self.assertFalse(any(record["open"] for record in browser.windows()))


class TestApiBrowserPanel(GuiCase):
    """Эндпоинты окна: гейт 18+ на открытии, захват адреса, воронка по адресу."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch("app.legal.legal_path",
                             return_value=self.dir / "legal.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        browser.reset()
        self.addCleanup(browser.reset)
        self.stub_backend()
        self.fake_system_browser()
        self.api = self.make_api()

    def stub_backend(self, window=None, error=None):
        patcher = mock.patch.dict(sys.modules,
                                  {"webview": stub_webview(window, error)})
        patcher.start()
        self.addCleanup(patcher.stop)
        return patcher

    def fake_system_browser(self):
        patcher = mock.patch.object(browser, "open_in_system_browser",
                                    side_effect=lambda url: {"ok": True})
        patcher.start()
        self.addCleanup(patcher.stop)
        return patcher

    def logs(self) -> list[str]:
        return self.api.poll(0)["logs"]

    def patch_fetch(self, snapshot):
        def fake(url, settings=None, on_progress=None, stop=None,
                 account_id=None, log=None, attempt_chain=False):
            if on_progress:
                on_progress(snapshot["total"], snapshot["total"])
            return snapshot
        patcher = mock.patch("app.gui.sources.fetch_snapshot", side_effect=fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def single_video_snapshot(self, url="https://youtube.com/watch?v=vidfixtur01"):
        snap = {"playlist": {"platform": "youtube", "remote_id": "vidfixtur01",
                             "title": "Одно видео", "description": None,
                             "kind": "video", "url": url, "item_count": 1,
                             "raw_json": "{}", "channel": None},
                "entries": [{"position": 1, "remote_id": "vidfixtur01",
                             "title": "Одно видео", "duration_s": 42,
                             "uploaded_at": None, "view_count": None,
                             "unavailable": False, "webpage_url": url,
                             "channel": None}],
                "total": 1, "url": url}
        return snap

    def test_open_platform_by_id_and_logs(self):
        window = FakeWindow(address="https://www.youtube.com/watch?v=abc")
        self.stub_backend(window)
        got = self.api.browser_open({"platform": "youtube"})
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["mode"], "webview")
        self.assertTrue(any("Окно площадки открыто" in line
                            for line in self.logs()))
        taken = self.api.browser_address({"id": got["id"]})
        self.assertEqual(taken["address"], "https://www.youtube.com/watch?v=abc")
        self.assertTrue(any("Адрес площадки взят" in line for line in self.logs()))
        closed = self.api.browser_close({"id": got["id"]})
        self.assertTrue(closed["ok"], closed)
        self.assertTrue(any("Окно площадки закрыто" in line
                            for line in self.logs()))

    def test_open_needs_platform_or_url(self):
        got = self.api.browser_open({})
        self.assertTrue(got.get("error"), got)
        self.assertEqual(browser.windows(), [], "отказ не создаёт окно")

    def test_open_adult_platform_blocked_until_18plus(self):
        """Гейт 18+ на открытии: до согласия окно не открывается, текст нейтрален."""
        url = "https://www.pornhub.com/view_video.php?viewkey=ph5f12ab34cd567"
        got = self.api.browser_open({"url": url})
        self.assertTrue(got.get("adult_confirm"), got)
        self.assertEqual(browser.windows(), [], "до согласия окно не открыто")
        # Публичный текст отказа: имя и домены взрослой площадки не звучат
        self.assertNotIn("pornhub", got["error"].lower())
        self.assertIn("18", got["error"])
        self.api.legal_accept({"adult": True})
        again = self.api.browser_open({"url": url})
        self.assertTrue(again["ok"], again)
        self.assertEqual(len(browser.windows()), 1)

    def test_open_adult_by_platform_id_blocked_until_18plus(self):
        got = self.api.browser_open({"platform": "pornhub"})
        self.assertTrue(got.get("adult_confirm"), got)
        self.assertNotIn("pornhub", got["error"].lower())

    def test_broken_backend_address_is_reported_as_not_received(self):
        window = FakeWindow(error=RuntimeError("бокенд не ответил"))
        self.stub_backend(window)
        got = self.api.browser_open({"platform": "youtube"})
        taken = self.api.browser_address({"id": got["id"]})
        self.assertEqual(taken["address"], "")
        self.assertIn(browser.NOT_RECEIVED, taken["note"])
        self.assertTrue(any("Адрес не получен" in line for line in self.logs()))
        # Значение адреса не выдумывается: журнал не пишет пустой адрес как «есть»
        self.assertFalse(any("Адрес площадки взят" in line
                             for line in self.logs()))

    def test_add_from_address_requires_a_real_address(self):
        self.assertTrue(self.api.browser_add_from_address({}).get("error"))
        bad = self.api.browser_add_from_address({"url": "file:///C:/x.mp4"})
        self.assertTrue(bad.get("error"), bad)
        self.assertIn("http", bad["error"])

    def test_add_from_address_runs_single_video_through_add_start(self):
        """«Скачать это видео» из панели = та же воронка, что по ссылке."""
        self.patch_fetch(self.single_video_snapshot())
        got = self.api.browser_add_from_address({
            "url": "https://youtube.com/watch?v=vidfixtur01", "mode": "partial"})
        self.assertTrue(got["ok"], got)
        flow = self.wait_phase(self.api, "confirm")
        self.assertEqual(flow["plan"]["counts"]["new_videos"], 1)
        self.assertTrue(self.api.add_confirm()["ok"])
        done = self.wait_phase(self.api, "done")
        self.assertEqual(done["result"]["stats"]["new_videos"], 1)
        self.assertEqual(self.api.poll(0)["stats"]["total"], 1)
        self.assertTrue(any("Добавление по адресу из окна площадки" in line
                            for line in self.logs()))

    def test_add_from_address_adult_blocks_until_18plus(self):
        """Адрес взрослой площадки из окна проходит через тот же гейт."""
        url = "https://www.pornhub.com/view_video.php?viewkey=ph5f12ab34cd567"
        got = self.api.browser_add_from_address({"url": url})
        self.assertTrue(got.get("adult_confirm"), got)
        self.assertEqual(self.api.poll(0)["add_flow"]["phase"], "idle",
                         "до согласия ничего не начинается")
        self.patch_fetch(self.single_video_snapshot(url))
        self.api.legal_accept({"adult": True})
        again = self.api.browser_add_from_address({"url": url})
        self.assertTrue(again["ok"], again)

    def test_add_from_address_playlist_shows_plan_dialog(self):
        snap = {"playlist": {"platform": "youtube", "remote_id": "PLfixtur0000000000000000000001",
                             "title": "Тестовый плейлист", "description": None,
                             "kind": "remote", "url": "https://youtube.com/playlist?list=PLfixtur0000000000000000000001",
                             "item_count": 2, "raw_json": "{}", "channel": None},
                "entries": [{"position": i, "remote_id": f"vid{i:09d}",
                             "title": f"Видео {i}", "duration_s": 30,
                             "uploaded_at": None, "view_count": None,
                             "unavailable": False,
                             "webpage_url": f"https://youtu.be/vid{i:09d}",
                             "channel": None} for i in (1, 2)],
                "total": 2,
                "url": "https://youtube.com/playlist?list=PLfixtur0000000000000000000001"}
        self.patch_fetch(snap)
        got = self.api.browser_add_from_address(
            {"url": snap["url"], "mode": "partial"})
        self.assertTrue(got["ok"], got)
        flow = self.wait_phase(self.api, "confirm")
        self.assertEqual(flow["plan"]["counts"]["new_videos"], 2)


class TestPanelTextsNeutral(BrowserCase):
    """Публичные тексты панели — нейтральные: имя и домены взрослой площадки не пишутся."""

    def test_window_title_is_neutral(self):
        for word in ("pornhub", "PornHub", "PH"):
            self.assertNotIn(word, browser.WINDOW_TITLE)

    def test_fallback_note_is_neutral(self):
        self.fake_system_browser()
        got = browser.open_system_browser("https://www.pornhub.com/view_video.php?viewkey=x")
        note = got["note"] + got["reason"] + got["url"]
        # URL человека — его собственный ввод; тексты программы нейтральны
        self.assertNotIn("PornHub", note)
        self.assertNotIn("PornHub", got["label"])

    def test_panel_markup_has_no_adult_name(self):
        root = Path(__file__).resolve().parent.parent / "ui_src"
        html = (root / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("pornhub", html.lower(),
                         "разметка окна не должна называть взрослую площадку")
        self.assertIn("Открыть площадку", html)
        self.assertIn("Забрать адрес", html)
        self.assertIn("Как источник", html)

    def test_panel_scripts_are_neutral(self):
        """Блок панели в скриптах — нейтральный (mock-данные каталога правит другой этап)."""
        js = (Path(__file__).resolve().parent.parent / "ui_src" / "app.js"
              ).read_text(encoding="utf-8")
        at = js.find("Забрать адрес")
        self.assertGreater(at, -1, "панель площадки не в скриптах")
        block = js[max(0, at - 1500): at + 1500]
        self.assertNotIn("pornhub", block.lower())
        self.assertIn(browser.NOT_RECEIVED, js)


if __name__ == "__main__":
    unittest.main()
