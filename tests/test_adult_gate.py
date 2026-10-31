"""M15: вкладка взрослой площадки скрыта до подтверждения 18+.

Что сверяет тест:
  * adult_tab: вкладка видна только при обоих условиях - факт «мне есть 18»
    (app/legal.py, датированное согласие) И включённый флаг настройки; флаг
    сам по себе доступа не даёт;
  * adult_tab_show без подтверждения отказывает (флаг не обходит гейт),
    adult_tab_hide выключает флаг, но НЕ снимает согласие человека;
  * гейты 18+ не ослаблены и не обойдены: add_start -> adult_confirm,
    каталог взрослой площадки -> need_adult, массовые операции -> adult_confirm;
  * реестр площадок (app/platforms.py) не переименован: внутренние id и домены
    те же (функционал сохраняется), нейтральной стала только подпись UI;
  * в ui_src нет имени и домена площадки; вкладка фильтруется по adult-флагу
    и появляется только после вызова adult_tab_show.
"""

import unittest
from pathlib import Path
from unittest import mock

from app import legal as legal_mod
from app import platforms
from tests.test_gui import GuiCase

UI = Path(__file__).resolve().parent.parent / "ui_src"
PH_URL = "https://www.pornhub.com/view_video.php?viewkey=ph698a3d3b0e335"


class AdultTabCase(GuiCase):
    def setUp(self):
        super().setUp()
        # Факты согласий - в профиле теста: без патча гейт читал бы настоящее
        # «мне есть 18» пользователя и не срабатывал бы.
        patcher = mock.patch("app.legal.legal_path",
                             return_value=self.dir / "legal.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.api = self.make_api()

    def logs(self) -> list[str]:
        return self.api.poll(0)["logs"]

    def flag(self) -> bool:
        return bool(self.api._current_settings().get("show_adult_tab"))

    def adult_row(self) -> int:
        """Строка взрослой площадки в индексе (для массовых операций)."""
        from app import repo
        vid, _created = repo.upsert_video(self.api.db.conn, {
            "platform": "pornhub", "remote_id": "phgate1",
            "title": "строка взрослой площадки", "channel": None})
        self.assertTrue(vid, "строка взрослой площадки не создана")
        return int(vid)


class TestAdultTabVisibility(AdultTabCase):
    def test_hidden_by_default(self):
        got = self.api.adult_tab()
        self.assertTrue(got["ok"], got)
        self.assertIs(got["confirmed"], False, "подтверждения 18+ нет")
        self.assertIs(got["shown"], False,
                      "флаг настройки по умолчанию выключен")
        self.assertIs(got["visible"], False,
                      "вкладка взрослой площадки скрыта до подтверждения")
        self.assertEqual(got["label"], "Взрослая площадка")

    def test_flag_alone_does_not_open(self):
        """Флаг без подтверждения - вкладка всё равно скрыта (гейт не обойдён)."""
        saved = self.api.save_setting({"key": "show_adult_tab", "value": True})
        self.assertIs(saved["settings"]["show_adult_tab"], True)
        got = self.api.adult_tab()
        self.assertIs(got["shown"], True)
        self.assertIs(got["confirmed"], False)
        self.assertIs(got["visible"], False,
                      "флаг настройки сам по себе доступ к взрослой площадке "
                      "не даёт")

    def test_show_without_confirmation_refused(self):
        refused = self.api.adult_tab_show()
        self.assertIs(refused.get("ok"), False, refused)
        self.assertIn("18+", refused.get("error", ""), refused)
        self.assertIs(refused["visible"], False)
        self.assertFalse(self.flag(),
                         "отказ не должен включать флаг настройки")
        self.assertIs(self.api.adult_tab()["visible"], False)
        self.assertTrue(any("18+" in line for line in self.logs()),
                        "отказ должен быть виден в журнале")

    def test_confirmation_then_show(self):
        accepted = self.api.legal_accept({"adult": True})
        self.assertTrue(accepted.get("adult_ok"), accepted)
        # Подтверждение само вкладку не показывает: нужен ещё флаг.
        between = self.api.adult_tab()
        self.assertIs(between["confirmed"], True)
        self.assertIs(between["visible"], False)

        shown = self.api.adult_tab_show({})
        self.assertTrue(shown["ok"], shown)
        self.assertIs(shown["visible"], True)
        self.assertTrue(self.flag())
        self.assertIs(self.api.adult_tab()["visible"], True)
        self.assertIn("18", "".join(self.logs()),
                      "подтверждение 18+ фиксируется в журнале")

    def test_hide_keeps_the_consent(self):
        self.api.legal_accept({"adult": True})
        self.api.adult_tab_show({})
        hidden = self.api.adult_tab_hide({})
        self.assertTrue(hidden["ok"], hidden)
        self.assertIs(hidden["visible"], False)
        self.assertIs(hidden["confirmed"], True,
                      "согласие человека не снимается скрытием вкладки")
        self.assertFalse(self.flag())
        self.assertIs(self.api.adult_tab()["visible"], False)
        # Гейт остаётся принятым: повторный show проходит без диалога.
        self.assertTrue(self.api.adult_tab_show({})["ok"])

    def test_label_neutral_for_both_languages(self):
        self.assertEqual(self.api.adult_tab()["label"], "Взрослая площадка")
        self.api.language_set("en")
        label = self.api.adult_tab()["label"]
        self.assertEqual(label, "Adult site")
        self.assertNotIn("pornhub", label.lower())
        self.assertNotIn("PornHub", label)


class TestAdultGatesUnchanged(AdultTabCase):
    """Гейты 18+ (добавление, каталог, массовые операции) - без изменений."""

    def test_add_start_still_asks_confirmation(self):
        refused = self.api.add_start({"url": PH_URL})
        self.assertIs(refused.get("adult_confirm"), True, refused)
        self.assertIn("18", refused.get("error", ""))
        # Открытие вкладки (флаг) не меняет гейт: подтверждение всё равно нужно.
        self.api.save_setting({"key": "show_adult_tab", "value": True})
        again = self.api.add_start({"url": PH_URL})
        self.assertIs(again.get("adult_confirm"), True,
                      "флаг вкладки не обходит возрастной гейт добавления")

    def test_catalog_gate_needs_confirmation(self):
        denied = self.api._cat_args({"platform": "pornhub"})
        self.assertIs(denied.get("need_adult"), True, denied)
        # Только подтверждение (не флаг) выпускает запрос к каталогу.
        self.api.save_setting({"key": "show_adult_tab", "value": True})
        self.assertIs(self.api._cat_args({"platform": "pornhub"}).get("need_adult"),
                      True)
        self.api.legal_accept({"adult": True})
        parsed = self.api._cat_args({"platform": "pornhub"})
        self.assertNotIn("need_adult", parsed, parsed)
        self.assertEqual(parsed["platform"], "pornhub")

    def test_bulk_gate_needs_confirmation(self):
        row = self.adult_row()
        refused = self.api.bulk_tags({"ids": [row], "add": ["метка"]})
        self.assertIs(refused.get("adult_confirm"), True, refused)
        self.assertIn("18", refused.get("error", ""))
        self.api.legal_accept({"adult": True})
        done = self.api.bulk_tags({"ids": [row], "add": ["метка"]})
        self.assertTrue(done.get("ok"), done)

    def test_registry_ids_and_domains_untouched(self):
        """Нейтральна только подпись: внутренние id/домены реестра те же."""
        ids = [item["id"] for item in platforms.PLATFORMS]
        self.assertIn("pornhub", ids)
        self.assertIn("youtube", ids)
        ph = platforms.by_id("pornhub")
        self.assertTrue(ph["adult"], "взрослый флаг реестра должен остаться")
        self.assertEqual(ph["label"], "PornHub",
                         "внутреннюю подпись реестра не переименовываем")
        hosts = [host for item in platforms.PLATFORMS
                 for host in item.get("domains", [])]
        self.assertTrue(any("pornhub.com" in host for host in hosts),
                        "домен реестра должен остаться: функционал площадки "
                        "не ломается нейтральным текстом")


class TestUiKeepsAdultNeutral(AdultTabCase):
    """UI: имени площадки нет, вкладка фильтруется по adult-факту."""

    def test_no_site_name_or_domain_in_ui(self):
        for name in ("app.js", "app.css", "index.html"):
            text = (UI / name).read_text(encoding="utf-8")
            self.assertNotIn("PornHub", text,
                             f"{name}: имя площадки в интерфейсе - публичный "
                             "текст должен быть нейтральным")
            self.assertNotIn("pornhub.com", text.lower(),
                             f"{name}: домен площадки в интерфейсе")

    def test_neutral_caption_present_and_no_static_tab(self):
        js = (UI / "app.js").read_text(encoding="utf-8")
        self.assertIn("Взрослая площадка", js,
                      "нейтральная подпись взрослой площадки потеряна")
        self.assertIn("Adult site", js, "нейтральная подпись для en потеряна")
        html = (UI / "index.html").read_text(encoding="utf-8")
        self.assertNotIn('data-tab="pornhub"', html,
                         "вкладка взрослой площадки не должна быть статичной "
                         "в разметке: до подтверждения её там нет")

    def test_tab_filtered_by_visibility_and_show_call(self):
        js = (UI / "app.js").read_text(encoding="utf-8")
        self.assertRegex(js, r"!p\.adult\s*\|\|\s*state\.adultVisible",
                         "вкладка взрослой площадки фильтруется по флагу "
                         "видимости (state.adultVisible)")
        self.assertRegex(js, r'call\("adult_tab"\)',
                         "UI должен спрашивать доступ к вкладке у бэка")
        self.assertRegex(js, r'call\("adult_tab_show"',
                         "появление вкладки - только после adult_tab_show")
        self.assertRegex(js, r"renderPlatformTabs\(\)",
                         "вкладки перерисовываются после смены доступа")

    def test_adult_dialog_text_neutral(self):
        js = (UI / "app.js").read_text(encoding="utf-8")
        self.assertIn("взрослой площадки", js,
                      "диалог подтверждения должен говорить нейтрально")

    def test_adult_tab_api_does_not_touch_gates(self):
        """adult_tab читает, а не пишет согласие: гейты остаются отказом."""
        state_before = legal_mod.state()
        self.api.adult_tab()
        self.api.adult_tab_show()
        self.assertEqual(legal_mod.state().get("adult_ok", ""),
                         state_before.get("adult_ok", ""),
                         "чтение доступа к вкладке не должно фиксировать "
                         "согласие 18+")

    def test_tab_state_survives_settings_rev(self):
        """Флаг меняет rev: окно перерисует вкладки, а не останется на старой."""
        before = self.api.get_initial()["settings_rev"]
        self.api.legal_accept({"adult": True})
        after = self.api.adult_tab_show({})["settings_rev"]
        self.assertGreater(after, before)


if __name__ == "__main__":
    unittest.main()
