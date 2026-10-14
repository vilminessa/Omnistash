"""Супервизор link: поднимает демон/будильник, если они упали."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import link_watch  # noqa: E402


class TestEnsure(unittest.TestCase):
    def test_starts_missing_daemon(self):
        with mock.patch.object(link_watch, "daemon_alive",
                               return_value=False), \
                mock.patch.object(link_watch, "wait_running",
                                  return_value=True), \
                mock.patch.object(link_watch, "_spawn_logged",
                                  return_value=111) as spawn:
            result = link_watch.ensure("pcA", "t", "1.2.3.4:18571", 18571)
        self.assertEqual(result["started"], ["serve(pid 111)"])
        args = spawn.call_args[0][0]
        self.assertIn("serve", args)
        self.assertIn("1.2.3.4:18571", args)
        self.assertIn("--token", args)

    def test_starts_missing_wait(self):
        with mock.patch.object(link_watch, "daemon_alive",
                               return_value=True), \
                mock.patch.object(link_watch, "wait_running",
                                  return_value=False), \
                mock.patch.object(link_watch, "_spawn_logged",
                                  return_value=222) as spawn:
            result = link_watch.ensure("pcA", "t", "1.2.3.4:18571", 18571)
        self.assertEqual(result["started"], ["wait(pid 222)"])
        self.assertIn("wait", spawn.call_args[0][0])

    def test_all_alive_is_noop(self):
        with mock.patch.object(link_watch, "daemon_alive",
                               return_value=True), \
                mock.patch.object(link_watch, "wait_running",
                                  return_value=True), \
                mock.patch.object(link_watch, "_spawn_logged") as spawn:
            result = link_watch.ensure("pcA", "t", "1.2.3.4:18571", 18571)
        self.assertEqual(result["started"], [])
        spawn.assert_not_called()

    def test_both_missing(self):
        with mock.patch.object(link_watch, "daemon_alive",
                               return_value=False), \
                mock.patch.object(link_watch, "wait_running",
                                  return_value=False), \
                mock.patch.object(link_watch, "_spawn_logged",
                                  return_value=333) as spawn:
            result = link_watch.ensure("pcA", "t", "1.2.3.4:18571", 18571)
        self.assertEqual(result["started"],
                         ["serve(pid 333)", "wait(pid 333)"])
        self.assertEqual(spawn.call_count, 2)


class TestCli(unittest.TestCase):
    def test_once_requires_args(self):
        with self.assertRaises(SystemExit):
            link_watch.main(["once"])        # без --id/--token/--peer


if __name__ == "__main__":
    unittest.main()
