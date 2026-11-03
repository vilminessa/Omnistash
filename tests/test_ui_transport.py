"""Страж S0: HTTP-транспорт клиента (docs/SERVER-CLIENT.md §3, §4).

Контракт «тот же фасад Api, только по HTTP» держится здесь: имена методов
сверяются с app/gui.py (AST), права - со списком скоупов в документе, а
подписи отказа - со словарём i18n. Удаление цели, «упрощение» транспорта или
выдуманный ответ вместо честной подписи станут красным тестом.

Что проверяет тест:
  * четыре списка в app.js (HTTP_METHODS / HTTP_DESKTOP / HTTP_LOCAL /
    HTTP_INTERNAL) ровно покрывают публичные методы Api и не изобретают
    целей, которых у бэка нет; ни одна цель не стоит в двух списках;
  * нативные цели (диалог папки, проводник, системный браузер, вход OAuth)
    не уходят по HTTP; предпочтения клиента (theme/font/language) не
    передаются серверу; bind_window не экспортируется;
  * транспорт: POST /api/<имя>, Bearer из localStorage, тело "{}" когда
    аргумента нет, credentials same-origin, адрес только свой (ни одного
    fetch на чужой хост);
  * коды ответа читаются честно: 401 привязка, 403 скоуп, 404 «не найдено»,
    409 «уже выполняется», 413 и 429 лимиты, 5xx ошибка - каждая подпись
    есть в словаре i18n с английским вариантом и совпадает с fallback в коде;
  * без ключа запросы не отправляются (не 120 пустых ответов на тик), причина
    видна в банере и в статус-строке, тик не перерисовывает список снимком;
  * порядок выбора моста: pywebview → HTTP → mock; превью не появляется,
    когда страницу отдаёт сервер;
  * разметка и CSS банера: live-регион, токены, никакого нового keyframes.
"""

import ast
import re
import unittest
from pathlib import Path

from app import i18n

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui_src"
JS = UI / "app.js"
CSS = UI / "app.css"
HTML = UI / "index.html"
BACKEND = ROOT / "app" / "gui.py"
DOC = ROOT / "docs" / "SERVER-CLIENT.md"

# Цели, которые по HTTP не выполняются: нативный диалог, проводник,
# системный браузер, окно площадки, вход OAuth, закрытие приложения.
NATIVE = {
    "pick_folder", "open_folder", "open_url", "open_file", "open_path",
    "open_log", "close", "browser_open", "browser_open_system_browser",
    "browser_address", "browser_close", "browser_add_from_address",
    "account_login_start", "account_login_stop", "account_capture_now",
    "account_visible", "oauth_start",
}

# Подписи отказа транспорта: как код читается в коде -> ключ словаря.
CODE_KEYS = {
    "status === 401": "http.pairing",
    "status === 403": "http.scope",
    "status === 404": "http.missing",
    "status === 409": "http.busy",
    "status === 413": "http.too_large",
    "status === 429": "http.rate",
    "status >= 500": "http.server",
    'code: "bad_json"': "http.bad_json",
    'code: "transport"': "http.offline",
    'code: "desktop"': "http.desktop",
    'code: "local"': "http.local",
}


def _clean(text: str) -> str:
    """Файл без комментариев: комментарии объясняют ловушки и не код."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


def api_public_names() -> set:
    """Публичные методы класса Api из app/gui.py (без импорта модуля)."""
    tree = ast.parse(BACKEND.read_text(encoding="utf-8"))
    cls = [node for node in tree.body
           if isinstance(node, ast.ClassDef) and node.name == "Api"]
    assert cls, "в app/gui.py нет класса Api"
    return {node.name for node in cls[0].body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not node.name.startswith("_")}


def js_list(js: str, name: str) -> list:
    match = re.search(rf"var {name} = \[(.*?)\];", js, re.DOTALL)
    assert match, f"{name} пропал из ui_src/app.js"
    return re.findall(r'"([A-Za-z0-9_]+)"', match.group(1))


def doc_scope_names(doc: str) -> set:
    """Имена целей из таблицы скоупов (§4) и из строк про локальное/внутреннее."""
    section = re.search(r"## 4\..*?(?=## 5\.)", doc, re.DOTALL)
    assert section, "в docs/SERVER-CLIENT.md нет раздела §4 со скоупами"
    text = section.group(0)
    names = set()
    for line in re.findall(r"^\| `[a-z_]+` \|.*$", text, re.MULTILINE):
        cells = line.split("|")
        names |= set(re.findall(r"`([a-z_]+)`", cells[-2]))
    for line in re.findall(r"^- `.*$", text, re.MULTILINE):
        names |= set(re.findall(r"`([a-z_]+)`", line))
    return names


class TestTransport(unittest.TestCase):
    def setUp(self):
        self.js = _clean(JS.read_text(encoding="utf-8"))
        self.css = _clean(CSS.read_text(encoding="utf-8"))
        self.html = _clean(HTML.read_text(encoding="utf-8"))
        self.doc = DOC.read_text(encoding="utf-8")
        self.methods = js_list(self.js, "HTTP_METHODS")
        self.desktop = js_list(self.js, "HTTP_DESKTOP")
        self.local = js_list(self.js, "HTTP_LOCAL")
        self.internal = js_list(self.js, "HTTP_INTERNAL")

    # ------------------------------ имена ------------------------------

    def test_lists_cover_backend_exactly_once(self):
        backend = api_public_names()
        every = self.methods + self.desktop + self.local + self.internal
        self.assertEqual(sorted(every), sorted(backend),
                         "списки транспорта разошлись с публичными методами Api")
        self.assertEqual(len(set(every)), len(every),
                         "одна цель стоит в двух списках транспорта")
        self.assertEqual(set(self.methods),
                         backend - set(self.desktop) - set(self.local)
                         - set(self.internal),
                         "HTTP-цели не равны «Api минус нативные и локальные»")

    def test_no_invented_targets(self):
        invented = sorted(set(self.methods) - api_public_names())
        self.assertEqual(invented, [],
                         "клиент зовёт цели, которых у бэка нет: " + ", ".join(invented))

    def test_native_targets_stay_off_http(self):
        self.assertEqual(set(self.desktop), NATIVE,
                         "список нативных целей изменился - сверьте §4 документа")
        self.assertTrue(set(self.desktop).isdisjoint(self.methods),
                        "нативная цель ушла по HTTP")

    def test_client_preferences_not_sent_to_server(self):
        self.assertEqual(set(self.local),
                         {"theme_set", "font_set", "language_set"},
                         "предпочтения клиента должны оставаться локальными (§4)")

    def test_internal_bridge_not_exported(self):
        self.assertEqual(self.internal, ["bind_window"])
        self.assertNotIn("bind_window", self.methods)
        self.assertNotIn('call("bind_window"', self.js)

    def test_scope_table_in_doc_matches_code(self):
        self.assertEqual(doc_scope_names(self.doc),
                         set(self.methods) | set(self.desktop) | set(self.local)
                         | set(self.internal),
                         "таблица скоупов в документе разошлась с ui_src/app.js")

    def test_read_scope_has_snapshots_and_card(self):
        # Снимок состояния, карточка и подписи - то, что читает любой клиент:
        # без этих целей UI не показывает ничего, даже с правом read.
        for name in ("poll", "get_initial", "list_videos", "get_video",
                     "get_thumbs", "ui_texts", "storages_list"):
            self.assertIn(name, self.methods, name + " пропал из HTTP-целей")

    # ------------------------------ транспорт ------------------------------

    def test_transport_is_post_with_bearer(self):
        self.assertRegex(self.js, r'method:\s*"POST"',
                         "вызов к серверу перестал быть POST")
        self.assertIn('"/api/" + name', self.js,
                      "маршрут /api/<имя> пропал из транспорта")
        self.assertRegex(self.js,
                         r'headers\.Authorization\s*=\s*"Bearer "\s*\+\s*token',
                         "ключ доступа не уходит в Authorization")
        self.assertIn('"omnistash.token"', self.js,
                      "токен больше не читается из localStorage")
        self.assertIn('credentials: "same-origin"', self.js,
                      "credentials снят: запрос мог бы уйти чужому хосту")

    def test_missing_argument_is_empty_object(self):
        # Тот же нюанс, что с pywebview: метод без параметров не должен
        # получать позиционный null.
        self.assertRegex(self.js,
                         r'\(arg === undefined \|\| arg === null\) \? "\{\}"',
                         "аргумент без значения снова едет как null")

    def test_only_own_origin_is_reachable(self):
        self.assertNotRegex(self.js, r"fetch\(\s*['\"]https?://",
                            "fetch на абсолютный адрес - чужой хост")
        self.assertNotRegex(self.js, r"fetch\(\s*['\"]//",
                            "fetch на протокол-относительный адрес - чужой хост")
        self.assertIn("location.origin", self.js,
                      "адрес сервера больше не берётся из origin")
        # HTTP включается по метке сервера, а не по протоколу: превью
        # подаётся статическим сервером (127.0.0.1 или file://) и должно
        # оставаться на мок-бэкенде, а не стать битым клиентом.
        self.assertIn("window.OMNISTASH_HTTP", self.js,
                      "серверная метка базы пропала: клиент не знает, с кем говорит")
        self.assertNotRegex(self.js, r'location\.protocol === "http:"',
                            "включение HTTP по протоколу: превью на сервере стало бы клиентом")
        base = re.search(r"function httpBaseUrl\(\)(.*?)\n  }", self.js, re.DOTALL)
        self.assertTrue(base, "httpBaseUrl пропал из app.js")
        self.assertRegex(base.group(1), r"if \(!mark\) return null;",
                         "без метки сервера база стала бы произвольной")

    def test_status_codes_are_honest(self):
        for marker, key in CODE_KEYS.items():
            self.assertIn(marker, self.js,
                          f"отказ {marker} больше не читается по подписи")
            self.assertTrue(i18n.has(key), f"{key} нет в словаре i18n")
            self.assertTrue(i18n.t(key, "ru"), f"{key} без русской подписи")
            en = i18n.t(key, "en")
            self.assertTrue(en, f"{key} без английского варианта")
            self.assertFalse(any(char in "абвгдежзийклмнопрстуфхцчшщэюя"
                                 for char in en), f"{key}: en с кириллицей")

    def test_fallback_texts_match_dictionary(self):
        pairs = re.findall(r't\(\s*"([a-z_.]+)"\s*,\s*"([^"]+)"', self.js)
        for key, fallback in pairs:
            if not key.startswith("http."):
                continue
            self.assertEqual(fallback, i18n.t(key, "ru"),
                             f"подпись в коде ({fallback}) не равна словарю {key}")

    def test_preview_mock_english_matches_dictionary(self):
        # Превью переключает язык своей картой (MOCK_EN): её подписи для
        # транспорта обязаны совпадать со словарём, иначе превью показывает
        # другой английский текст, чем окно.
        block = re.search(r"var MOCK_EN\s*=\s*\{(.*?)\};", self.js, re.DOTALL)
        self.assertTrue(block, "карта MOCK_EN пропала из превью")
        used = set(re.findall(r't\(\s*"(http\.[a-z_]+)"', self.js))
        mock = dict(re.findall(r'"(http\.[a-z_]+)"\s*:\s*"([^"]+)"',
                               block.group(1)))
        self.assertEqual(sorted(mock), sorted(used),
                         "карта MOCK_EN не покрывает подписи транспорта (§4 i18n)")
        for key, text in mock.items():
            self.assertEqual(text, i18n.t(key, "en"),
                             f"английская подпись {key} в превью ≠ словарю")

    def test_denied_targets_answer_with_a_reason(self):
        # Молчаливый null вместо подписи - это «кнопка не работает без
        # объяснения»; отказ обязан читаться как текст.
        self.assertRegex(self.js,
                         r'HTTP_DESKTOP\.forEach[\s\S]*code:\s*"desktop"',
                         "нативная цель отвечает молча")
        self.assertRegex(self.js,
                         r'HTTP_LOCAL\.forEach[\s\S]*code:\s*"local"',
                         "локальная цель отвечает молча")

    def test_unpaired_does_not_spam_the_server(self):
        post = re.search(r"function httpPost\([\s\S]*?\n  }\n", self.js)
        self.assertTrue(post, "httpPost пропал из app.js")
        body = post.group(0)
        self.assertLess(body.index("unpaired"), body.index("fetch("),
                        "без ключа запрос всё равно отправляется")
        self.assertIn("httpProblem.shown", body,
                      "причина привязки не сохраняется для подписи")

    def test_bad_json_is_named_not_swallowed(self):
        self.assertRegex(self.js, r"code:\s*\"bad_json\"",
                         "не-JSON ответ снова становится пустым результатом")

    def test_tick_shows_the_problem_not_a_fake_snapshot(self):
        tick = re.search(r"function tick\(\)[\s\S]*?\n  }\n", self.js)
        self.assertTrue(tick, "tick пропал из app.js")
        body = tick.group(0)
        self.assertRegex(body, r"if \(snap\.error\) \{",
                         "ошибка транспорта в тике больше не видна")
        self.assertLess(body.index("snap.error"), body.index("applySnapshot"),
                        "тик перерисовывает список битым снимком")
        self.assertRegex(body, r'\$\("status"\)\.textContent = snap\.error',
                         "причина не попадает в статус-строку (live-регион)")

    # ------------------------------ выбор моста ------------------------------

    def test_bridge_prefers_window_then_http_then_mock(self):
        bridge = re.search(r"function bridge\(\)[\s\S]*?\n  }\n", self.js)
        self.assertTrue(bridge, "bridge пропал из app.js")
        body = bridge.group(0)
        self.assertRegex(body, r"window\.pywebview && window\.pywebview\.api")
        self.assertRegex(body, r"return http \|\| mock;",
                         "порядок мостов изменился: HTTP или превью вперёд")
        self.assertLess(body.index("pywebview"), body.index("return http"),
                        "окно не получает мост раньше HTTP-клиента")

    def test_http_client_installed_before_preview(self):
        self.assertRegex(self.js,
                         r"if \(!window\.pywebview && httpMode\(\)\) \{",
                         "HTTP-клиент больше не ставится до превью")
        self.assertRegex(self.js,
                         r"function installPreview\(\)[\s\S]*?"
                         r"window\.pywebview \|\| mock \|\| http",
                         "превью могло встать поверх HTTP-клиента")
        # Превью-баннер показывается только installPreview: серверная страница
        # не получает подпись «данные вымышленные».
        preview = re.search(r"function installPreview\(\)[\s\S]*?\n    bootOnce\(\);",
                            self.js)
        self.assertTrue(preview, "installPreview пропал из app.js")
        self.assertIn('$("preview-badge").hidden = false', preview.group(0))
        self.assertNotRegex(self.js[:self.js.index("function installPreview")],
                            r'\$\("preview-badge"\)\.hidden = false',
                            "выдуманные данные объявляются до installPreview")

    def test_badge_markup_is_a_live_region(self):
        self.assertRegex(self.html, r'id="client-badge"[^>]*role="status"',
                         "баннер клиента не объявляет состояние")
        self.assertRegex(self.html, r'id="client-badge"[^>]*aria-live="polite"')
        self.assertRegex(self.html, r'id="client-badge"[^>]*hidden')
        self.assertNotRegex(self.html, r'id="client-badge"[^>]*aria-live="assertive"',
                            "баннер привязки не должен кричать на каждую попытку")

    def test_badge_css_reads_tokens(self):
        block = re.search(r"#client-badge \{([^}]*)\}", self.css)
        self.assertTrue(block, "правило #client-badge пропало из app.css")
        css = block.group(1)
        for token in ("--surface-2", "--info", "--info-line", "--fs-cap",
                      "--r-2"):
            self.assertIn(f"var({token})", css, f"{token} не читается в банере")
        self.assertNotRegex(css, r"#[0-9a-fA-F]{3,6}",
                            "литеральный цвет в банере (DESIGN-UI §2)")
        self.assertNotIn("transition", css,
                         "банер не анимируется: приходит через textContent (§4.3)")

    def test_badge_shown_when_unpaired_and_hidden_on_success(self):
        self.assertRegex(self.js, r"function clientBadge\(",
                         "функция банера пропала")
        self.assertRegex(self.js, r'badge\.hidden = !text',
                         "банер не гасится по состоянию")
        self.assertRegex(self.js, r'clientBadge\(""\)',
                         "успешный ответ не убирает банер привязки")
        self.assertRegex(self.js, r"clientBadge\(problem\.error\)|clientBadge\(snap\.error\)",
                         "причина отказа не показывается в банере")

    def test_no_new_keyframes_in_css(self):
        names = set(re.findall(r"@keyframes\s+([A-Za-z0-9_-]+)\s*\{", self.css))
        self.assertEqual(names, {"pulse", "cat-sweep"},
                         "новый @keyframes в шаге S0 запрещён (§4 DESIGN-UI)")


if __name__ == "__main__":
    unittest.main()
