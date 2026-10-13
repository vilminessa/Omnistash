"""B4: mDNS-обнаружение участников (опциональный zeroconf).

Библиотека не обязательна: без неё модуль честно отказывается и пишет
причину; сами тесты (кроме путей отказа) пропускаются.
"""

import os
import time
import unittest
from unittest import mock

from app.mesh import mdns


class TestAvailability(unittest.TestCase):
    def test_available_flag_and_service_type(self):
        self.assertIsInstance(mdns.available(), bool)
        self.assertEqual(mdns.SERVICE_TYPE, "_omnistash._tcp.local.")

    def test_own_name_short_and_dotted_free(self):
        name = mdns.own_name()
        self.assertTrue(name)
        self.assertLessEqual(len(name), 32)
        self.assertNotIn(".", name)

    def test_lan_ip_shape(self):
        self.assertRegex(mdns.lan_ip(), r"^\d{1,3}(\.\d{1,3}){3}$")


class TestHonestRefusal(unittest.TestCase):
    def test_starts_refuse_without_library(self):
        """Нет zeroconf - старты возвращают False с готовой подсказкой."""
        notes = []
        with mock.patch.object(mdns, "AVAILABLE", False), \
                mock.patch.object(mdns, "IMPORT_ERROR", "нет пакета"):
            announcer = mdns.Announcer(38599, "", log=notes.append)
            self.assertFalse(announcer.start())
            browser = mdns.Browser(lambda *args: None, log=notes.append)
            self.assertFalse(browser.start())
        joined = " ".join(notes)
        self.assertIn("pip install zeroconf", joined)
        self.assertIn("hello/DHT", joined)


@unittest.skipUnless(mdns.available(), "zeroconf не установлена")
class TestWithLibrary(unittest.TestCase):
    def test_info_shape(self):
        info = mdns.service_info(38574, nick="Иван")
        self.assertEqual(info.port, 38574)
        self.assertTrue(info.name.endswith(mdns.SERVICE_TYPE))
        self.assertEqual(info.properties[b"v"], b"1")
        self.assertEqual(info.properties[b"nick"], "Иван".encode("utf-8"))

    def test_announcer_start_stop(self):
        notes = []
        announcer = mdns.Announcer(38599, "Тест", log=notes.append)
        started = announcer.start()
        try:
            if not started:
                # 5353 занят или нет прав - честная причина в журнале
                self.assertTrue(any("mDNS" in line for line in notes), notes)
                return
            self.assertTrue(announcer.running)
            self.assertEqual(announcer.port, 38599)
        finally:
            announcer.stop()
        self.assertFalse(announcer.running)

    def test_browser_event_delivers_ipv4_only(self):
        """Событие Added резолвится и доставляется; IPv6 отфильтрован."""
        class FakeInfo:
            port = 38574
            properties = {b"nick": "Сосед".encode("utf-8")}

            @staticmethod
            def parsed_addresses():
                return ["192.168.0.5", "fe80::1"]

        class FakeZc:
            @staticmethod
            def get_service_info(type_, name, timeout=None):
                return FakeInfo()

        found = []
        browser = mdns.Browser(lambda *args: found.append(args))
        browser._on_event(FakeZc(), mdns.SERVICE_TYPE,
                          "Сосед._omnistash._tcp.local.",
                          mdns.ServiceStateChange.Added)
        self.assertEqual(found, [("192.168.0.5", 38574, "Сосед")])

    def test_browser_event_removed_is_ignored(self):
        found = []
        browser = mdns.Browser(lambda *args: found.append(args))
        browser._on_event(None, mdns.SERVICE_TYPE, "x",
                          mdns.ServiceStateChange.Removed)
        self.assertEqual(found, [])


@unittest.skipUnless(mdns.available() and os.environ.get("OMNISTASH_LIVE"),
                     "живой mDNS - только с OMNISTASH_LIVE")
class TestLiveDiscovery(unittest.TestCase):
    def test_announcer_seen_by_browser(self):
        found = []
        announcer = mdns.Announcer(38599, "Тест")
        self.assertTrue(announcer.start(), "живой старт объявления")
        browser = mdns.Browser(lambda addr, port, nick: found.append(
            (addr, port, nick)))
        try:
            self.assertTrue(browser.start(), "живой старт поиска")
            deadline = time.time() + 8
            while time.time() < deadline and not found:
                time.sleep(0.2)
        finally:
            browser.stop()
            announcer.stop()
        self.assertTrue(found, "собственный сервис должен найтись")
        self.assertTrue(any(port == 38599 for _addr, port, _nick in found))


if __name__ == "__main__":
    unittest.main()
