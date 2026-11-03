"""SV-C S4: страница воспроизведения клиента (docs/SERVER-CLIENT.md §3, §6, §7).

Страница живёт на сервере (`/watch/<video_id>`), грузит только свой адрес, и
каждый отказ назван подписью сервера или словаря. Вендор `hls.js` лежит в
репозитории и закреплён по sha256: рантайм ничего не скачивает из сети, и
supply-chain проверен пином, а не «доверием к CDN».

Что проверяется:
  * пин вендора: sha256 файла совпадает с `hls.js.meta.json`, лицензия
    Apache-2.0 по метадате пакета (не MIT), версия закреплена в URL источника,
    sourcemap убрана (сервер не отдаёт .map - браузер не запросит файл,
    которого нет), внешние CDN-хосты в файле отсутствуют;
  * разметка: токены §2.7 без литералов, ни одного @keyframes и перехода,
    role=status у строки состояния, native controls, субтитры по умолчанию
    скрыты (кнопка с aria-pressed), узкий экран, ссылка назад;
  * транспорт зеркала окна: POST /api/<имя>, Bearer из localStorage,
    позиционный [arg], "{}" когда аргумента нет, credentials same-origin,
    ни одного чужого хоста и ни одной нативной цели (open_file остаётся окну);
  * честность плеера: плейлист проверяется до attach (отказ сервера назван),
    отказ «браузер не играет HLS» назван, аудио играется прямо байтами,
    ссылки файлов только на те маршруты, которые сервер раздаёт;
  * подписи страниц и ключей есть в словаре на обоих языках;
  * проводка бэкенда: маршруты /watch и карта статики, id файлов в карточке
    (плеер адресует /media и /subs по file_id), get_video под правом read.
"""

import hashlib
import json
import re
import unittest
from pathlib import Path

from app import i18n
from app.http import scopes
from app.http import server as serve_http

SRC = Path(__file__).resolve().parent.parent
UI = SRC / "ui_src"
WATCH_HTML = UI / "watch.html"
WATCH_JS = UI / "watch.js"
VENDOR = UI / "vendor" / "hls.js"
VENDOR_META = UI / "vendor" / "hls.js.meta.json"
DOC = SRC / "docs" / "SERVER-CLIENT.md"
APP_JS = UI / "app.js"
REPO = SRC / "app" / "repo.py"
UI_PY = SRC / "app" / "ui.py"

# Хосты CDN: рантайм обязан грузить только свой адрес, а пин проверяется по
# sha256 из репозитория.
CDN = re.compile(r"cdn\.jsdelivr|unpkg\.net|unpkg\.com|cdnjs", re.IGNORECASE)


class TestVendorPin(unittest.TestCase):
    """Supply chain: вендор закреплён по sha256, лицензия - по метадате пакета."""

    def setUp(self):
        self.meta = json.loads(VENDOR_META.read_text(encoding="utf-8"))
        self.vendor = VENDOR.read_text(encoding="utf-8")

    def test_sha256_matches_the_file(self):
        digest = hashlib.sha256(VENDOR.read_bytes()).hexdigest()
        self.assertEqual(self.meta["sha256"], digest,
                         "пин вендора разошёлся с файлом: сборка выдаст не то, что в репозитории")

    def test_version_and_source_are_pinned_together(self):
        self.assertEqual(self.meta["package"], "hls.js")
        self.assertRegex(self.meta["version"], r"^\d+\.\d+\.\d+$")
        self.assertIn("@" + self.meta["version"], self.meta["source"],
                      "URL источника не закреплён той же версией, что пин")
        self.assertRegex(self.meta["fetched"], r"^\d{4}-\d{2}-\d{2}$")

    def test_license_is_the_package_metadata(self):
        """registry.npmjs.org говорит Apache-2.0 - MIT в пине было бы выдумкой."""
        self.assertEqual(self.meta["license"], "Apache-2.0")
        self.assertNotIn("MIT", json.dumps(self.meta))
        self.assertIn("провенанс", self.meta["publish"],
                      "публичная цепочка поставки не названа (npm attestations)")

    def test_sourcemap_removed_and_no_cdn_hosts(self):
        self.assertNotIn("sourceMappingURL", self.vendor,
                         "ссылка на .map: браузер спросит файл, которого сервер не отдаёт")
        self.assertFalse(CDN.search(self.vendor),
                         "в вендоре остался CDN-хост: рантайм грузил бы чужой адрес")
        self.assertGreater(len(self.vendor), 100_000,
                           "вендор урезан: страница получила бы битый плеер")
        self.assertIn("Hls", self.vendor[-200:],
                      "файл не объявляет глобальный Hls - watch.js не найдёт плеер")


class TestWatchMarkup(unittest.TestCase):
    """Разметка плеера: токены, доступность, узкий экран, ни одного нового движения."""

    def setUp(self):
        self.html = WATCH_HTML.read_text(encoding="utf-8")

    def test_assets_are_relative_to_own_origin(self):
        self.assertIn('href="/app.css"', self.html)
        self.assertIn('src="/vendor/hls.js"', self.html)
        self.assertIn('src="/watch.js"', self.html)
        self.assertFalse(re.search(r'(?:href|src)="https?://', self.html),
                         "страница ссылается на чужой хост")

    def test_no_new_animation_no_transition_no_hex(self):
        # DESIGN-UI §7: keyframes только прежние (pulse, cat-sweep); страница
        # плеера не добавает своих, переходы - только из токенов (их здесь нет).
        self.assertNotIn("@keyframes", self.html)
        self.assertNotIn("transition:", self.html)
        self.assertFalse(re.search(r"#[0-9a-fA-F]{3,6}\b", self.html),
                         "литеральный цвет вместо токена §2.7")
        self.assertIn("var(--", self.html, "компоновка не опирается на токены")

    def test_status_is_live_and_controls_are_native(self):
        status = re.search(r'id="watch-status"[^>]*>', self.html)
        self.assertTrue(status, "строка состояния пропала")
        self.assertIn('role="status"', status.group(0))
        self.assertIn('aria-live="polite"', status.group(0))
        video = re.search(r"<video[^>]*>", self.html)
        self.assertTrue(video and "controls" in video.group(0),
                        "плеер без native controls - клавиатура не управляет")
        audio = re.search(r"<audio[^>]*>", self.html)
        self.assertTrue(audio and "controls" in audio.group(0) and "hidden" in audio.group(0),
                        "аудио-строка без отдельного плеера")

    def test_captions_button_toggle_and_back_link(self):
        button = re.search(r'id="watch-captions"[^>]*>', self.html)
        self.assertTrue(button, "кнопка субтитров пропала")
        self.assertIn('aria-pressed="false"', button.group(0),
                      "кнопка без состояния нажатия - скрытые субтитры не видны")
        self.assertIn("hidden", button.group(0),
                      "кнопка без субтитров показана - пустая право-кнопка")
        self.assertIn('href="/"', self.html, "со страницы плеера нет пути назад")

    def test_narrow_screen_and_noscript_are_honest(self):
        self.assertIn("@media (max-width:", self.html, "узкий экран не описан (M6)")
        self.assertIn("<noscript>", self.html)
        self.assertIn("JavaScript", self.html,
                      "noscript не объясняет, почему страница не работает")


class TestWatchTransport(unittest.TestCase):
    """JS плеера: тот же транспорт, что окно, и только свои маршруты."""

    def setUp(self):
        self.js = WATCH_JS.read_text(encoding="utf-8")

    def test_transport_mirrors_the_window_bridge(self):
        self.assertIn('method: "POST"', self.js)
        self.assertIn('"/api/" + name', self.js)
        self.assertIn('Bearer ', self.js)
        self.assertIn('"omnistash.token"', self.js)
        self.assertIn("JSON.stringify([arg])", self.js,
                      "аргумент снова едет kwargs-объектом: сервер вызвал бы метод не так, как окно")
        self.assertIn('? "{}"', self.js,
                      "метод без параметров снова получил бы позиционный null")
        self.assertIn('credentials: "same-origin"', self.js)

    def test_no_foreign_hosts_and_no_native_targets(self):
        self.assertFalse(re.search(r"https?://", self.js),
                         "в JS плеера появился чужой адрес: рантайм скачал бы что-то из сети")
        for target in ("open_file", "pick_folder", "open_path", "oauth_start"):
            self.assertNotIn(target, self.js,
                             f"нативная цель {target} у клиента нет - её решает окно")
        self.assertIn("get_video", self.js)
        self.assertIn("ui_texts", self.js)

    def test_playback_is_honest_before_and_after(self):
        # Плейлист проверяется до плеера: 400 с подписью кодека не должен
        # повисеть в hls.js как «загрузка».
        order = self.js.index("function playHls")
        body = self.js[order:order + 900]
        self.assertIn("fetch(url", body)
        self.assertIn("response.status === 200", body)
        self.assertIn("attachPlaylist", body)
        self.assertIn("canPlayType", self.js, "Safari без hls.js остался бы без плеера")
        self.assertIn("browser_hls", self.js,
                      "браузер без HLS молчал бы вместо названной причины")
        self.assertIn("/media/", self.js, "аудио не играется прямо байтами (§6 п.4)")
        self.assertIn('say(t("watch.failed"', self.js,
                      "фатальная ошибка hls.js не названа строкой состояния")

    def test_file_links_only_served_routes(self):
        self.assertIn('file.kind === "video"', self.js)
        self.assertIn('file.kind === "subtitle"', self.js)
        self.assertIn('file.kind === "thumbnail"', self.js)
        self.assertNotIn("sidecar", self.js,
                         "ссылка на маршрут, который сервер не раздаёт (/media sidecar не читает)")
        self.assertIn("watch.hash_none", self.js,
                      "хеш без значения назван «не вычислен», а не выдуман")
        self.assertIn("watch.gone", self.js,
                      "пропавший файл не назван пропавшим")

    def test_captions_default_hidden(self):
        self.assertIn('track.mode = "hidden"', self.js,
                      "субтитры включены по умолчанию - человек не просил их")
        self.assertIn('showing', self.js)
        self.assertIn("http.subs_missing", self.js,
                      "отсутствие субтитров молчит вместо подписи")

    def test_token_absent_is_pairing_not_an_empty_player(self):
        self.assertIn("http.no_token", self.js)
        self.assertIn('"pairing"', self.js)

    def test_every_signature_key_is_in_the_dictionary(self):
        keys = set(re.findall(r'(?<![A-Za-z])t\("([a-z_.]+)"', self.js))
        keys |= set(re.findall(r'data-l="([a-z_.]+)"', WATCH_HTML.read_text(encoding="utf-8")))
        self.assertTrue(keys, "подписи плеера исчезли из watch.js")
        for key in sorted(keys):
            self.assertTrue(i18n.has(key), f"{key}: подписи нет в словаре")
            self.assertTrue(i18n.t(key, "ru"), f"{key}: нет русской подписи")
            en = i18n.t(key, "en")
            self.assertTrue(en, f"{key}: нет английского варианта")
            self.assertFalse(any(char in "абвгдежзийклмнопрстуфхцчшщэюя"
                                 for char in en), f"{key}: en с кириллицей")


class TestBackendWiring(unittest.TestCase):
    """Бэкенд раздаёт страницу, файлы и id файлов; права те же, что в таблице."""

    def test_ui_builds_watch_page_and_assets(self):
        text = UI_PY.read_text(encoding="utf-8")
        self.assertIn("def build_watch_page", text)
        self.assertIn("def watch_page_available", text)
        self.assertIn("def asset", text)
        self.assertIn("_with_marker", text,
                      "страница плеера без метки сервера - клиент не знает свой адрес")

    def test_static_map_names_existing_files_only(self):
        for route, (name, ctype) in serve_http.CLIENT_ASSETS.items():
            self.assertTrue((UI / name).is_file(), f"{route}: файл {name} не в ui_src")
            self.assertTrue(ctype, f"{route}: тип не задан")
        self.assertEqual(sorted(serve_http.CLIENT_ASSETS),
                         sorted(["/app.css", "/watch.js", "/vendor/hls.js"]),
                         "карта статики изменилась без проверки разметки страницы")

    def test_server_dispatches_watch_page(self):
        text = (SRC / "app" / "http" / "server.py").read_text(encoding="utf-8")
        self.assertIn('path.startswith("/watch/")', text)
        self.assertIn("http.watch_missing", text,
                      "отказ непостроенной страницы называет причину в журнале и теле")

    def test_detail_gives_file_ids(self):
        """Плеер адресует /media и /subs по file_id - карточка обязана их отдавать."""
        self.assertRegex(REPO.read_text(encoding="utf-8"),
                         r"SELECT id, kind, path",
                         "video_detail больше не отдаёт id файлов: клиент не построит маршрут")
        # Пути дисков в карточке остаются для окна; наружу по байтовым маршрутам
        # уходит только id (стражи S2 проверяют отсутствие путей в раздачах).

    def test_watch_targets_are_gated_by_scopes(self):
        self.assertEqual(scopes.scope_for("get_video"), "read",
                         "карточка плеера - право read (она часть библиотеки)")
        self.assertEqual(serve_http.route_scope("hls"), "watch")
        self.assertEqual(serve_http.route_scope("media"), "watch")
        self.assertEqual(serve_http.route_scope("subs"), "watch")
        self.assertEqual(serve_http.route_scope("thumb"), "read")

    def test_open_from_library_goes_to_the_page(self):
        js = APP_JS.read_text(encoding="utf-8")
        body = re.search(r"function playVideo\(id\)(.*?)\n  }", js, re.DOTALL)
        self.assertTrue(body, "playVideo пропал из app.js")
        self.assertIn("httpMode()", body.group(1))
        self.assertIn('"/watch/" + id', body.group(1),
                      "клиент больше не переходит на страницу плеера")
        self.assertIn('call("open_file"', body.group(1),
                      "окно потеряло системный плеер (нативная цель остаётся)")

    def test_doc_names_the_real_license(self):
        doc = DOC.read_text(encoding="utf-8")
        self.assertIn("Apache-2.0", doc)
        self.assertNotIn("hls.js — MIT", doc,
                         "документ называет hls.js MIT: метадата пакета говорит Apache-2.0")
        self.assertFalse(re.search(r"hls\.js`?\s*\(MIT\)", doc))


class TestWatchGlue(unittest.TestCase):
    """Скан против артефактов генерации в новых файлах шага."""

    def test_no_cjk_and_no_script_glue(self):
        for path in (WATCH_HTML, WATCH_JS, VENDOR_META, DOC):
            text = path.read_text(encoding="utf-8")
            self.assertFalse([char for char in text
                              if "\u3400" <= char <= "\u9fff" or "\uac00" <= char <= "\udfff"],
                             f"{path.name}: CJK-вставка")
            glue = re.search(r"[A-Za-z][а-яё]|[а-яё][A-Za-z]", text)
            if path == DOC:
                continue          # документ описывает идентификаторы по имени
            if glue is not None:
                self.fail(f"{path.name}: склейка латиницы с кириллицей - {glue.group(0)}")


if __name__ == "__main__":
    unittest.main()
