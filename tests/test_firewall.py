"""Правило брандмауэра: когда создаётся, когда уже есть, когда отказ.

netsh/PowerShell мокается: офлайн-тест не трогает реальные правила
(на CI и у разработчика свои).
"""

import unittest
from unittest import mock

from app.mesh import firewall


class TestFirewallRules(unittest.TestCase):
    def test_rule_name_shape(self):
        self.assertEqual(firewall._rule_name(38574, "TCP"),
                         "Omnistash mesh 38574 TCP")

    def test_manual_command_covers_both_protocols(self):
        command = firewall.manual_command(38574)
        self.assertIn("New-NetFirewallRule", command)
        self.assertIn("-Protocol TCP", command)
        self.assertIn("-Protocol UDP", command)
        self.assertIn("-LocalPort 38574", command)
        self.assertIn("-Profile Private", command)

    def test_existing_rules_are_quiet(self):
        logs = []
        with mock.patch.object(firewall, "_powershell",
                               return_value="exists") as ps:
            self.assertTrue(firewall.ensure_rules(38574, logs.append))
        self.assertEqual(ps.call_count, 2, "по проверке на протокол")
        self.assertEqual(logs, [], "правила уже есть - молчим")

    def test_created_rules_are_logged(self):
        logs = []
        with mock.patch.object(firewall, "_powershell",
                               side_effect=["created", "created"]):
            self.assertTrue(firewall.ensure_rules(38574, logs.append))
        self.assertEqual(len(logs), 2)
        self.assertIn("TCP/38574", logs[0])
        self.assertIn("UDP/38574", logs[1])

    def test_denied_falls_back_to_manual_command(self):
        """Нет прав администратора - НЕ тихо: журнал с готовой командой."""
        logs = []
        with mock.patch.object(
                firewall, "_powershell",
                side_effect=RuntimeError("Access is denied")):
            self.assertFalse(firewall.ensure_rules(38574, logs.append))
        self.assertEqual(len(logs), 2, "по протоколу")
        self.assertIn("администратора", logs[0])
        self.assertIn("New-NetFirewallRule", logs[0],
                      "журнал обязан дать готовую команду")
        self.assertIn("Access is denied", logs[0])

    def test_non_windows_is_noop(self):
        with mock.patch.object(firewall, "os") as fake_os:
            fake_os.name = "posix"
            with mock.patch.object(firewall, "_powershell") as ps:
                self.assertTrue(firewall.ensure_rules(38574, lambda m: None))
                ps.assert_not_called()


if __name__ == "__main__":
    unittest.main()
