"""Страж S5: клиентская UI по HTTP с скоупами (docs/SERVER-CLIENT.md §4, §8).

Что держит этот тест:
  * зеркало прав в клиенте совпадает с бэком: `HTTP_SCOPE_OF` = `scopes.SCOPE_OF`,
    `HTTP_ROUTE_SCOPE` = карта байтовых маршрутов, `ACCESS_SCOPES` = скоупы
    токена без `desktop`, `LANG_IDS` = язык из словаря - цель не может
    исчезнуть из одного места;
  * права ключа читаются у сервера (`GET /whoami`) и подписывают недоступные
    цели ДО запроса: клиент не отправляет вызов, которому право не выдано, и
    отказ читается текстом, а не серой кнопкой и не молчанием (§8);
  * предпочтения клиента (тема, шрифт, язык) остаются локальными: в клиентском
    режиме они не уходят `save_setting`, язык читается через `ui_texts` с
    `request.language` - настройка сервера не меняется;
  * вместо проводника папки - поле пути (`storage_set_path` по тексту), пустое
    поле названо причиной;
  * DESIGN-UI №5 (табовая семантика строки вкладок) и №25 (второй индикатор
    «где я») закрыты: роли, `aria-selected`, `aria-controls`, roving tabindex,
    стрелки, активная вкладка с двумя индикаторами, текстовый «Где я»;
  * M13: `queue_retry` с `attempt_chain=True` и честная сводка, какая дорога
    прогоняется;
  * подписи новых captions покрыты словарём на обоих языках и совпадают с
    fallback-ами в коде; превью (MOCK_EN) держит те же английские строки;
  * CSS: только токены, никаких новых `@keyframes`, состояние не гасится
    `opacity`.
"""

import re
import unittest
from pathlib import Path

from app import i18n
from app.http import scopes as serve_scopes
from app.http import server as serve_http
from tests.test_gui import GuiCase

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui_src"
JS = UI / "app.js"
CSS = UI / "app.css"
HTML = UI / "index.html"
DOC = ROOT / "docs" / "SERVER-CLIENT.md"


def _clean(text: str) -> str:
    """Файл без комментариев: комментарии объясняют ловушки и не код."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


def js_list(js: str, name: str) -> list:
    match = re.search(rf"var {name} = \[(.*?)\];", js, re.DOTALL)
    assert match, f"{name} пропал из ui_src/app.js"
    return re.findall(r'"([A-Za-z0-9_]+)"', match.group(1))


def js_scope_map(js: str) -> dict:
    match = re.search(r"var HTTP_SCOPE_OF = \{(.*?)\};", js, re.DOTALL)
    assert match, "зеркало скоупов цели пропало из ui_src/app.js"
    return dict(re.findall(r'"([A-Za-z0-9_]+)":\s*"([a-z_]+)"', match.group(1)))


def js_route_map(js: str) -> dict:
    match = re.search(r"var HTTP_ROUTE_SCOPE = \{(.*?)\};", js, re.DOTALL)
    assert match, "карта прав байтовых маршрутов пропала из ui_src/app.js"
    return dict(re.findall(r"([a-z]+):\s*\"([a-z_]+)\"", match.group(1)))


def js_fallbacks(js: str) -> dict:
    """Подписи `t("key", "fallback")`: значение может собираться конкатенацией."""
    out = {}
    for match in re.finditer(r't\(\s*"([a-z_.]+)",\s*((?:"[^"]*"\s*\+?\s*)+)', js):
        out[match.group(1)] = "".join(re.findall(r'"([^"]*)"', match.group(2)))
    return out


def js_mock_english(js: str) -> dict:
    """Карта MOCK_EN превью: те же английские строки, что словарь окна."""
    block = re.search(r"var MOCK_EN\s*=\s*\{(.*?)\};", js, re.DOTALL)
    assert block, "карта MOCK_EN пропала из превью"
    out = {}
    for match in re.finditer(r'"([a-z_.]+)":\s*((?:"[^"]*"\s*\+?\s*)+)',
                             block.group(1)):
        out[match.group(1)] = "".join(re.findall(r'"([^"]*)"', match.group(2)))
    return out


def function_body(js: str, name: str) -> str:
    match = re.search(rf"function {name}\([\s\S]*?\n  \}}\n", js)
    assert match, f"{name} пропал из ui_src/app.js"
    return match.group(0)


# Подписи клиента (S5): ключ -> обязан быть на обоих языках.
CLIENT_KEYS = [
    "http.scope_denied", "http.desktop", "http.scope", "http.offline",
    "http.bad_json", "access.granted", "access.missing", "access.unknown",
    "access.none", "access.native", "ui.where", "ui.where.pool",
    "ui.where.platform", "queue.retry.label", "queue.chain.label",
    "queue.chain.tip", "queue.chain.on", "queue.chain.off",
    "prefs.local.note", "prefs.local.denied", "storage.path.label",
    "storage.path.apply", "storage.path.tip", "storage.path.none",
    "storage.add.label", "storage.check.label", "storage.client.note",
]

# Нативные цели, подписанные в разметке и в разметке JS-виджетов.
NATIVE_MARKED = ["pick_folder", "open_folder", "open_url", "open_log",
                 "browser_open", "browser_open_system_browser",
                 "browser_address", "browser_close", "account_login_start",
                 "account_login_stop", "account_capture_now",
                 "account_visible"]


class TestScopeMirror(unittest.TestCase):
    def setUp(self):
        self.js = _clean(JS.read_text(encoding="utf-8"))
        self.css = _clean(CSS.read_text(encoding="utf-8"))
        self.html = _clean(HTML.read_text(encoding="utf-8"))
        self.doc = DOC.read_text(encoding="utf-8")

    # --------------------------- зеркало прав ---------------------------

    def test_scope_map_matches_the_backend_exactly(self):
        self.assertEqual(js_scope_map(self.js), serve_scopes.SCOPE_OF,
                         "зеркало скоупов в клиенте разошлось с app/http/scopes.py")

    def test_http_methods_are_the_scoped_targets(self):
        methods = js_list(self.js, "HTTP_METHODS")
        self.assertEqual(set(methods), set(js_scope_map(self.js)),
                         "список HTTP-целей разошёлся с картой скоупов")
        self.assertEqual(len(methods), len(set(methods)),
                         "одна цель стоит в списке дважды")

    def test_access_scopes_are_the_token_scopes_without_native(self):
        listed = js_list(self.js, "ACCESS_SCOPES")
        self.assertEqual(set(listed), set(serve_scopes.SCOPES) - {"desktop"},
                         "клиент перечисляет права иначе, чем реестр токенов")
        self.assertNotIn("desktop", listed,
                         "право desktop подписывалось бы как «не выдан скоуп», "
                         "а цель невозможна на клиенте вовсе")

    def test_byte_route_rights_match_the_server_map(self):
        self.assertEqual(js_route_map(self.js),
                         {route: serve_http.route_scope(route)
                          for route in serve_http.BYTE_ROUTES},
                         "карта прав байтовых маршрутов разошлась с сервером")

    def test_language_ids_match_the_dictionary(self):
        self.assertEqual(js_list(self.js, "LANG_IDS"),
                         list(i18n.language_ids()),
                         "клиент нормализует язык иначе, чем app/i18n.py")

    # --------------------------- локальные предпочтения ---------------------------

    def test_client_preferences_set_locally_not_on_the_server(self):
        self.assertEqual(js_list(self.js, "PREF_FIELDS"),
                         ["theme", "font_family", "font_size", "font_scale",
                          "language"],
                         "список локальных предпочждений изменился (§8)")
        handler = re.search(r"input\.addEventListener\(\"change\"[\s\S]*?\n        \}\);",
                            self.js)
        self.assertTrue(handler, "обработчик полей карточки настроек пропал")
        body = handler.group(0)
        self.assertLess(body.index("PREF_FIELDS"), body.index("saveSetting("),
                        "локальное поле карточки снова уходит save_setting: "
                        "клиент менял бы вид окна сервера")
        self.assertIn("prefStore(key, value)", body)

    def test_chrome_reads_local_preferences(self):
        chrome = function_body(self.js, "applyChrome")
        for key in ("theme", "font_size", "font_scale", "font_family"):
            self.assertIn(f'chromeSetting("{key}"', chrome,
                          f"{key} больше не читается из localStorage клиента")

    def test_language_is_read_for_the_client_only(self):
        sync = function_body(self.js, "syncLanguage")
        self.assertIn("httpMode() ? { language: clientLanguage() }", sync,
                      "клиент не просит подписи своего языка")
        self.assertIn('call("ui_texts", ask)', sync)

    def test_where_indicator_names_the_current_place(self):
        where = function_body(self.js, "whereIndicator")
        self.assertIn('t("ui.where", "Где я:")', where)
        self.assertIn("$(\"where-am-i\")", where)
        self.assertRegex(self.html, r'id="where-am-i"[^>]*role="status"')
        self.assertRegex(self.html, r'id="where-am-i"[^>]*aria-live="polite"')
        # Узкий экран: индикатор виден, когда строка вкладок не помещается.
        self.assertRegex(self.css, r"(?m)^\.where\s*\{[^}]*display:\s*none")
        self.assertRegex(self.css,
                         r"@media \(max-width: 900px\) \{[\s\S]*\.where \{\s*"
                         r"display:\s*block")


class TestClientAccess(unittest.TestCase):
    def setUp(self):
        self.js = _clean(JS.read_text(encoding="utf-8"))
        self.css = _clean(CSS.read_text(encoding="utf-8"))
        self.html = _clean(HTML.read_text(encoding="utf-8"))

    def test_rights_are_read_from_the_own_address(self):
        fetch = function_body(self.js, "fetchAccess")
        self.assertIn('httpBaseUrl() + "/whoami"', fetch,
                      "права ключа читаются не со своего адреса")
        self.assertIn('headers.Authorization = "Bearer " + token', fetch)
        self.assertIn('credentials: "same-origin"', fetch)
        self.assertNotRegex(self.js, r'fetch\(\s*["\']https?://',
                            "запрос на чужой хост - supply chain")

    def test_unread_rights_are_named_not_invented(self):
        band = function_body(self.js, "accessBand")
        for key in ("access.unknown", "access.none", "access.granted",
                    "access.missing"):
            self.assertIn(f't("{key}"', band, f"{key}: строка прав его не называет")
        self.assertRegex(self.html, r'id="access-band"[^>]*role="status"',
                         "строка прав не объявляется AT")
        self.assertRegex(self.html, r'id="access-band"[^>]*aria-live="polite"')
        self.assertRegex(self.html, r'id="access-band"[^>]*hidden')
        rule = re.search(r"(?m)^\.access-band\s*\{([^}]*)\}", self.css)
        self.assertTrue(rule, "нет правила .access-band")
        body = rule.group(1)
        for token in ("--surface-2", "--info", "--info-line", "--fs-cap"):
            self.assertIn(f"var({token})", body, f"{token} не читается")
        self.assertNotRegex(body, r"#[0-9a-fA-F]{3,6}", "литеральный цвет")
        self.assertNotIn("transition", body, "строка прав не анимируется")

    def test_denied_call_is_not_sent_to_the_server(self):
        call = function_body(self.js, "call")
        self.assertLess(call.index("accessReason"), call.index("bridge()"),
                        "цель без права всё равно уходит по HTTP")
        self.assertIn('code: HTTP_DESKTOP.indexOf(name) >= 0 ? "desktop"',
                      call, "отказ нативной цели теряется в пути отказа")
        self.assertIn("scope", call)

    def test_click_on_a_denied_target_explains_itself(self):
        guard = re.search(r'document\.addEventListener\("click"[\s\S]*?\n  \}, true\);',
                          self.js)
        self.assertTrue(guard, "страж кликов по недоступной цели пропал")
        body = guard.group(0)
        self.assertIn("stopImmediatePropagation", body,
                      "обработчик кнопки всё равно вызывается после отказа")
        self.assertIn("toast(", body, "причина отказа не показывается")
        self.assertIn("aria-disabled", body)

    def test_captioned_control_is_not_greyed(self):
        apply = function_body(self.js, "applyAccess")
        self.assertIn('setAttribute("aria-disabled"', apply)
        self.assertIn("access-note", apply)
        self.assertIn('closest(ACCESS_CONTAINERS)', apply,
                      "пояснение ставится не в контейнер контролов")
        note = re.search(r"(?m)^\.access-note\s*\{([^}]*)\}", self.css)
        self.assertTrue(note, "нет правила .access-note")
        body = note.group(1)
        for token in ("--surface-2", "--info-line", "--r-2", "--fs-cap"):
            self.assertIn(f"var({token})", body, f"{token} не читается в пояснении")
        self.assertNotIn("opacity", body, "пояснение гасит текст (замена №18)")
        denied = re.search(r"(?m)^\.btn\.no-access\s*\{([^}]*)\}", self.css)
        self.assertTrue(denied, "нет правила для недоступной кнопки")
        self.assertIn("var(--line-strong)", denied.group(1),
                      "состояние меняется только цветом без подписи и формы")
        self.assertNotIn("opacity", denied.group(1),
                         "недоступная кнопка гасится opacity (серая кнопка)")

    def test_every_scope_has_a_captioned_target(self):
        text = self.html + JS.read_text(encoding="utf-8")
        needs = set(re.findall(r'data-need="([a-z_]+)"', text))
        self.assertEqual(needs, set(serve_scopes.SCOPES) - {"desktop"},
                         "не все скоупы подписаны в интерфейсе: сравьте §4")

    def test_native_targets_are_marked(self):
        text = self.html + JS.read_text(encoding="utf-8")
        marked = set(re.findall(r'data-native="([a-z_]+)"', text))
        self.assertTrue(set(NATIVE_MARKED) <= marked,
                        f"нативные цели не подписаны: {sorted(set(NATIVE_MARKED) - marked)}")
        self.assertTrue(marked <= serve_scopes.NATIVE,
                        "подписана цель, которой нет в списке нативных")

    def test_playback_needs_the_watch_scope(self):
        self.assertIn('data-need="watch"', JS.read_text(encoding="utf-8"))
        play = function_body(self.js, "playVideo")
        self.assertLess(play.index("routeReason"), play.index('location.href'),
                        "клиент идёт на /watch без права на байты")
        self.assertIn("toast(blocked", play, "отказ по праву не показывается")

    # --------------------------- подписи ---------------------------

    def test_client_captions_are_covered_and_match_the_code(self):
        fallbacks = js_fallbacks(self.js)
        for key in CLIENT_KEYS:
            self.assertTrue(i18n.has(key), f"{key} нет в словаре")
            self.assertTrue(i18n.t(key, "en"), f"{key} без английской подписи")
            self.assertFalse(any(char in "абвгдежзийклмнопрстуфхцчшщэюя"
                                 for char in i18n.t(key, "en")),
                             f"{key}: английская подпись с кириллицей")
            fallback = fallbacks.get(key)
            if fallback is not None:
                self.assertEqual(fallback, i18n.t(key, "ru"),
                                 f"подпись в коде ({fallback}) ≠ словарю {key}")

    def test_markup_labels_are_covered_by_the_dictionary(self):
        keys = set(re.findall(r'data-l(?:-tip)?="([^"]+)"', self.html))
        new = {key for key in keys if key.startswith(("queue.", "storage."))}
        self.assertTrue(new, "разметка повтора и пути не помечена ключами")
        for key in new:
            self.assertTrue(i18n.has(key), f"{key} нет в словаре")
            self.assertTrue(i18n.t(key, "en"), f"{key} без английского варианта")

    def test_preview_mock_covers_the_new_captions(self):
        mock = js_mock_english(self.js)
        for key in CLIENT_KEYS:
            self.assertIn(key, mock, f"превью не покрывает подпись {key}")
            self.assertEqual(mock[key], i18n.t(key, "en"),
                             f"английская подпись {key} в превью ≠ словарю")


class TestTabsAndKeyboard(unittest.TestCase):
    def setUp(self):
        self.js = _clean(JS.read_text(encoding="utf-8"))
        self.css = _clean(CSS.read_text(encoding="utf-8"))
        self.html = _clean(HTML.read_text(encoding="utf-8"))

    def test_tablist_roles_and_panel_links(self):
        self.assertRegex(self.html, r'id="tabs"[^>]*role="tablist"',
                         "строка вкладок не объявлена tablist (замена №5)")
        self.assertRegex(self.html, r'id="tabs"[^>]*aria-label="')
        tabs = re.findall(r'<button class="tab[^>]*>', self.html)
        self.assertEqual(len(tabs), 4, "статических вкладок стало другое число")
        for tab in tabs:
            self.assertIn('role="tab"', tab)
            self.assertIn('aria-controls="panel-', tab)
            self.assertIn('aria-selected="', tab)
            self.assertIn('tabindex="', tab)
        panels = re.findall(r'<section class="panel[^>]*>', self.html)
        self.assertEqual(len(panels), 5, "панелей стало другое число")
        for panel in panels:
            self.assertIn('role="tabpanel"', panel)
            self.assertIn('tabindex="0"', panel)

    def test_platform_tabs_join_the_tablist_flattened(self):
        self.assertRegex(self.html, r'id="platform-tabs"[^>]*role="presentation"',
                         "обёртка вкладок площадок стоит в дереве AT как "
                         "чужой узел между tablist и tabs")
        build = re.search(r'var box = \$\("platform-tabs"\);[\s\S]*?\n        \}\)\.join',
                          self.js)
        self.assertTrue(build, "построитель вкладок площадок пропал")
        body = build.group(0)
        self.assertIn('role="tab"', body)
        self.assertIn('aria-controls="panel-platform"', body)
        self.assertIn('aria-selected="false"', body)
        self.assertIn('tabindex="-1"', body)

    def test_active_state_is_applied_to_every_tab(self):
        sync = function_body(self.js, "syncTabs")
        self.assertIn('setAttribute("aria-selected"', sync)
        self.assertIn('setAttribute("aria-current"', sync)
        self.assertIn("tab.tabIndex = on ? 0 : -1", sync,
                      "roving tabindex потерян: обход Tabs дал бы все вкладки")
        self.assertRegex(self.js, r"if \(name === \"platform\" && tabKey\) "
                                  r"state\.platformTab = tabKey",
                         "активная вкладка площадки снова не помечается (№25)")

    def test_arrow_keys_move_between_tabs(self):
        keyboard = function_body(self.js, "bindTabKeyboard")
        for key in ('"ArrowRight"', '"ArrowLeft"', '"Home"', '"End"'):
            self.assertIn(key, keyboard, f"обход вкладок потерял {key}")
        self.assertIn("activateTab", keyboard, "стрелка не переключает панель")
        self.assertIn("bindTabKeyboard()", self.js, "обход вкладок не привязан")

    def test_active_tab_has_two_visible_indicators(self):
        rule = re.search(r"\.tab\.active\s*\{([^}]*)\}", self.css)
        self.assertTrue(rule, "нет правила активной вкладки")
        body = rule.group(1)
        self.assertIn("color: var(--text)", body, "подпись активной вкладки не поднимается")
        self.assertIn("border-bottom-color: var(--accent)", body,
                      "underline активной вкладки потерян (№25)")
        self.assertIn("background: var(--selected)", body,
                      "второй индикатор активного таба потерян (NN/g ≥2)")

    def test_tree_marks_the_current_area(self):
        self.assertRegex(self.html, r'id="tree"[^>]*role="navigation"',
                         "боковая область не объявлена навигацией (§5.5)")
        self.assertRegex(self.html, r'id="tree"[^>]*aria-label="')
        item = re.search(r"function item\([\s\S]*?\n    \}", self.js)
        self.assertTrue(item, "построитель записей дерева пропал")
        self.assertIn('aria-current="true"', item.group(0),
                      "текущая область дерева не читается AT (№25)")
        self.assertIn("whereIndicator();", self.js.split("function renderTree")[1])


class TestStoragePathAndRetry(unittest.TestCase):
    def setUp(self):
        self.js = _clean(JS.read_text(encoding="utf-8"))
        self.html = _clean(HTML.read_text(encoding="utf-8"))

    def test_client_has_a_path_field_instead_of_a_folder_dialog(self):
        widget = function_body(self.js, "storagesHtml")
        self.assertIn("httpMode()", widget, "виджет хранилищ не различает режимы")
        self.assertIn("data-storage-path-input", widget)
        self.assertIn("data-storage-path-apply", widget)
        self.assertIn('data-native="pick_folder"', widget,
                      "кнопка проводника в окне не подписана как нативная")
        add = function_body(self.js, "pickAndAddStorage")
        self.assertLess(add.index("httpMode()"), add.index("pickFolder()"),
                        "клиент снова зовёт диалог папки сервера")
        self.assertIn('t("storage.path.none"', add,
                      "пустой путь читался бы как тихий отказ")

    def test_wizard_offers_the_path_field_to_a_client(self):
        self.assertRegex(self.html, r'id="wizard-client"')
        self.assertRegex(self.html, r'id="wizard-path"')
        self.assertRegex(self.html, r'id="wizard-pick"[^>]*data-native="pick_folder"')
        wizard = function_body(self.js, "updateWizard")
        self.assertIn("pick.hidden = httpMode()", wizard)
        self.assertIn("clientRow.hidden = !httpMode()", wizard)

    def test_retry_passes_the_chain_flag_and_names_the_road(self):
        handler = re.search(r'\$\("queue-retry-btn"\)\.addEventListener\("click"'
                            r"[\s\S]*?\n    \}\);", self.js)
        self.assertTrue(handler, "кнопка повтора упавших потеряна")
        body = handler.group(0)
        self.assertIn('call("queue_retry", { attempt_chain: chain })', body,
                      "M13: attempt_chain не передаётся бэкенду")
        self.assertIn("queue.chain.on", body)
        self.assertIn("queue.chain.off", body,
                      "сводка не называет, какая дорога прогоняется")
        self.assertRegex(self.html, r'id="queue-chain"[^>]*data-l-tip="queue\.chain\.tip"')
        self.assertRegex(self.html, r'id="queue-chain-wrap"[^>]*hidden')

    def test_mock_reports_the_same_chain_result(self):
        mock = re.search(r"queue_retry: function \(req\) [\s\S]*?\n      \},", self.js)
        self.assertTrue(mock, "мок повтора упавших не найден")
        self.assertIn("attempt_chain: !!(req && req.attempt_chain)", mock.group(0),
                      "мок превью выдумывает результат повтора без флага")

    def test_path_browse_button_is_the_server_explorer(self):
        field = re.search(r'else if \(field\.type === "path"\) \{([\s\S]*?)\n    \}',
                          self.js)
        self.assertTrue(field, "поле пути в карточке настроек пропало")
        self.assertIn('data-native="pick_folder"', field.group(1))
        browse = re.search(r"#settings-body \[data-browse\][\s\S]*?pickFolder\(\)",
                           self.js)
        self.assertTrue(browse, "кнопка «…» поля пути осталась мёртвой целью")
        self.assertIn("pickFolder()", browse.group(0))
        self.assertIn("httpMode()", browse.group(0),
                      "клиент зовёт проводник сервера: нативная цель не подписана")


class TestLanguageOverrideOnBackend(GuiCase):
    def test_ui_texts_language_reads_without_changing_the_window(self):
        api = self.make_api()
        got = api.ui_texts({"language": "en"})
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["language"], "en")
        self.assertEqual(got["texts"]["tab.library"], "Library")
        self.assertEqual(api.ui_texts()["language"], "ru",
                         "чтение подписей на другом языке сменило язык окна сервера")
        self.assertEqual(api._ui_language(), "ru",
                         "настройка языка сервера изменилась - клиент менял бы "
                         "окно сервера")

    def test_broken_client_language_returns_russian(self):
        api = self.make_api()
        got = api.ui_texts({"language": "klingon"})
        self.assertEqual(got["language"], "ru")
        self.assertEqual(got["texts"]["tab.library"], "Библиотека")


class TestCssBudget(unittest.TestCase):
    def setUp(self):
        self.css = _clean(CSS.read_text(encoding="utf-8"))
        self.js = _clean(JS.read_text(encoding="utf-8"))

    def test_no_new_keyframes(self):
        names = set(re.findall(r"@keyframes\s+([A-Za-z0-9_-]+)\s*\{", self.css))
        self.assertEqual(names, {"pulse", "cat-sweep"},
                         "новый @keyframes в шаге S5 запрещён (§8)")

    def test_new_rules_do_not_add_untoken_motion(self):
        for selector in (".access-band", ".where", ".access-note",
                         ".btn.no-access"):
            rule = re.search(r"(?m)^" + re.escape(selector) + r"\s*\{([^}]*)\}",
                             self.css)
            self.assertTrue(rule, f"нет правила {selector}")
            body = rule.group(1)
            self.assertNotRegex(body, r"#[0-9a-fA-F]{3,6}",
                                f"{selector}: литеральный цвет (§2 DESIGN-UI)")
            self.assertNotIn("transition", body,
                             f"{selector}: переход без токена (§4.2)")
            self.assertNotIn("animation", body, f"{selector}: цикл без токена")

    def test_generated_texts_have_no_cjk(self):
        """Артефакты генерации (иероглифы в русском тексте) становятся тестом."""
        for path in (JS, CSS, HTML, ROOT / "app/i18n.py", ROOT / "app/gui.py",
                     ROOT / "app/http/server.py", DOC, Path(__file__)):
            text = path.read_text(encoding="utf-8")
            hits = [f"{number}: {char}" for number, line in enumerate(
                text.splitlines(), 1) for char in line
                if "\u3400" <= char <= "\u9fff" or "\uac00" <= char <= "\udfff"]
            self.assertEqual(hits, [], f"{path.name}: CJK в тексте {hits[:4]}")

    def test_cyrillic_not_glued_with_latin(self):
        """Склейка латиницы с кириллицей в сгенерированном тексте - красный тест."""
        glue = re.compile(r"[A-Za-z][а-яё]|[а-яё][A-Za-z]")
        skip = ("http.", "storage.", "prefs.", "queue.", "ui.where", "access.",
                "data-", "aria-", "localStorage", "DPAPI", "GET", "POST",
                "save_setting", "pick_folder", "attempt_chain", "routeReason",
                "tabIndex", "whoami", "scope", "watch", "read", "mark",
                "sources", "mesh", "accounts", "server_config", "desktop",
                "hls", "media", "subs", "thumb", "ETag", "sha256", "json",
                "api", "app.js", "app.css", "index.html", "i18n", "PREF_FIELDS",
                "HTTP_", "LANG_IDS", "ACCESS_", "route_scope", "BYTE_ROUTES")
        hits = []
        for path in (JS, CSS, HTML, ROOT / "app/i18n.py", ROOT / "app/gui.py",
                     ROOT / "app/http/server.py", DOC):
            text = path.read_text(encoding="utf-8")
            for match in glue.finditer(text):
                fragment = text[max(0, match.start() - 25):match.end() + 25]
                if any(token in fragment for token in skip):
                    continue
                hits.append((path.name, fragment.replace("\n", " ")))
        self.assertEqual(hits, [], f"склейка латиницы с кириллицей: {hits[:6]}")


class TestDocContract(unittest.TestCase):
    def test_doc_names_the_rights_route_and_captions(self):
        doc = DOC.read_text(encoding="utf-8")
        self.assertIn("GET /whoami", doc, "§3 не называет чтение прав ключа")
        self.assertIn("не выдан скоуп", doc, "§8 не называет подпись цели без права")
        self.assertIn("доступно только на сервере", doc)
        self.assertIn("Где я", doc, "§8 не называет индикатор «где я»")
        self.assertIn("attempt_chain", doc, "§8 не называет M13")
        self.assertIn("aria-selected", doc, "§8 не называет табовую семантику (№5)")
