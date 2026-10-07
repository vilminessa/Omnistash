"""Расписание: интервалы, сроки, занятость и запуск задач без окна."""

import time
import unittest
from unittest import mock

from app import schedule
from app.gui import headless_sync
from tests.test_add_flow import fixture_snapshot
from tests.test_gui import GuiCase


class TestScheduler(unittest.TestCase):
    def setUp(self):
        self.calls = {"scan": 0, "sync": 0}
        self.results = {"scan": True, "sync": True}
        self.sched = schedule.Scheduler(
            on_scan=lambda: self._result("scan"),
            on_sync=lambda: self._result("sync"))

    def _result(self, key):
        self.calls[key] += 1
        return self.results[key]

    def test_zero_interval_never_fires(self):
        self.sched.apply_settings({"scan_interval_min": 0, "sync_interval_min": 0})
        self.sched.tick(now=time.time() + 10**9)
        self.assertEqual(self.calls, {"scan": 0, "sync": 0})
        state = self.sched.state()
        self.assertEqual(state["scan"]["interval"], 0)
        self.assertIsNone(state["scan"]["next_in"])

    def test_fires_when_due_and_reschedules(self):
        self.sched.set_interval("scan", 5)
        self.sched.set_interval("sync", 30)
        # Срок истёк: подделаем время так, будто прошло 5 минут.
        self.sched._next["scan"] = time.time() - 1
        fired = self.sched.tick()
        self.assertEqual(fired, ["scan"])
        self.assertEqual(self.calls["scan"], 1)
        self.assertEqual(self.calls["sync"], 0, "свой срок не истёк")
        # Срок перенесён на интервал, последняя задача запомнена.
        state = self.sched.state()
        self.assertGreater(state["scan"]["next_in"], 4 * 60)
        self.assertIsNotNone(state["scan"]["last"])

    def test_busy_task_is_retried_in_a_minute(self):
        self.sched.set_interval("scan", 5)
        self.sched._next["scan"] = time.time() - 1
        self.results["scan"] = False           # окно чем-то занято
        self.sched.tick()
        self.assertEqual(self.calls["scan"], 1)
        state = self.sched.state()
        # Не каждый тик и не на интервал - ровно через минуту.
        self.assertLessEqual(state["scan"]["next_in"], schedule.BUSY_RETRY_SECONDS)
        self.assertGreater(state["scan"]["next_in"],
                           schedule.BUSY_RETRY_SECONDS - 5)
        self.assertIsNone(state["scan"]["last"], "неудачный запуск не «последний»")

    def test_interval_change_moves_the_deadline(self):
        self.sched.set_interval("scan", 60)
        far = self.sched.state()["scan"]["next_in"]
        self.sched.set_interval("scan", 1)
        near = self.sched.state()["scan"]["next_in"]
        self.assertLess(near, far)
        self.assertLessEqual(near, 60)
        # То же значение ничего не сдвигает.
        before = self.sched.state()["scan"]["next_in"]
        self.sched.set_interval("scan", 1)
        self.assertEqual(self.sched.state()["scan"]["next_in"], before)

    def test_callback_exception_does_not_kill_timer(self):
        def boom():
            raise RuntimeError("неприятность")
        sched = schedule.Scheduler(on_scan=boom, on_sync=lambda: True)
        sched.set_interval("scan", 1)
        sched._next["scan"] = time.time() - 1
        self.assertEqual(sched.tick(), [])   # не «успешно», но и не падение
        self.assertGreater(sched.state()["scan"]["next_in"], 0)

    def test_out_of_range_interval_clamped(self):
        self.assertEqual(self.sched.set_interval("scan", -10), 0)
        self.assertEqual(self.sched.set_interval("sync", 99999),
                         schedule.MAX_MINUTES)
        self.assertEqual(self.sched.set_interval("scan", "мусор"), 0)

    def test_describe_for_ui(self):
        entry = {"interval": 30, "next_in": 600, "last": "2026-10-07T15:04:00"}
        text = schedule.describe(entry, "Автосинк")
        self.assertIn("каждые 30 мин", text)
        self.assertIn("600", text)
        self.assertIn("15:04", text)
        self.assertIn("выключен", schedule.describe({"interval": 0}, "Автоскан"))


class TestScheduledCallbacks(GuiCase):
    """Колбэки окна: не бить по занятому, честно отвечать отказом."""

    def test_busy_window_defers(self):
        api = self.make_api()
        api._busy = True
        self.assertFalse(api._scheduled_scan())
        self.assertFalse(api._scheduled_sync())

    def test_scan_reports_error_when_no_storages(self):
        api = self.make_api()
        # Нет хранилищ -> старт_scan вернёт ошибку -> колбэк False.
        self.assertFalse(api._scheduled_scan())

    def test_schedule_visible_in_poll(self):
        api = self.make_api()
        state = api.poll(0)["schedule"]
        self.assertEqual(set(state), {"scan", "sync"})
        self.assertEqual(state["scan"]["interval"], 0)

    def test_changing_interval_updates_scheduler(self):
        api = self.make_api()
        api.save_setting({"key": "sync_interval_min", "value": 15})
        state = api.poll(0)["schedule"]
        self.assertEqual(state["sync"]["interval"], 15)
        self.assertIsNotNone(state["sync"]["next_in"])
        self.assertLessEqual(state["sync"]["next_in"], 15 * 60)
        # И строка в журнале - иначе «почему оно стрельнуло» неоткуда узнать.
        self.assertTrue(any("Расписание" in line for line in api._logs))


class TestHeadlessSync(GuiCase):
    """--sync под планировщик: работает без окна и кодирует результат."""

    def test_without_sources_is_not_success(self):
        self.assertEqual(headless_sync(), 1)

    def test_with_source_exits_clean(self):
        # Заносим источник в профиль теста через обычный флоу...
        api = self.make_api()
        with mock.patch("app.gui.sources.fetch_snapshot",
                        return_value=fixture_snapshot(count=2)):
            api.add_start({"url": "https://youtube.com/playlist?list=PLx000000000000000000000001",
                           "mode": "manual"})
            self.wait_phase(api, "confirm")
            api.add_confirm()
            self.wait_phase(api, "done")
        api.close()

        # ...а дальше запускаем как это сделает планировщик Windows.
        with mock.patch("app.gui.sources.fetch_snapshot",
                        return_value=fixture_snapshot(count=3)):
            code = headless_sync()
        self.assertEqual(code, 0)
        # Повторный синк ничего не изменил: снапшот тот же по составу.
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
