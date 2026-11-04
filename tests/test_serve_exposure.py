"""Страж S6: режимы экспозиции, адресный гейт и привязка по короткому коду.

Документ (docs/SERVER-CLIENT.md §5, §7) требует, чтобы «наружу» было
настраиваемым, а не молча включаемым, и чтобы отказ назывался причиной. Здесь
сверяется:

  * `app/http/bind.py`: режимы (`loopback`, `lan`, `tailscale`, `wildcard`),
    согласие как условие выхода, allowlist для `0.0.0.0`, битый элемент
    списка = отказ, адрес не найден = отказ, loopback всегда свой;
  * `app/http/pair.py`: код 6 цифр или 4 слова из того же списка слов, что
    mesh SAS; одноразовый; срок; в памяти только SHA-256; пресеты прав без
    `desktop`; тормоз перебора (лимит попыток -> растущая задержка с
    потолком); журнал не содержит значения кода и значения токена;
  * слушатель: `POST /pair` без ключа выдаёт токен, `GET /pair` -> 405,
    неверный или показанный ранее код -> 401 с подписью, перебор -> 429 с
    `retry_in`, реестра кодов нет -> 409 с причиной, адрес вне allowlist ->
    403 до раздачи страницы и байтов, `0.0.0.0` без allowlist -> слушатель
    не поднят (ServeError с причиной);
  * фасад `Api`: согласие и значение кода принимает/отдаёт только окно
    (`app/http/context.py`), по HTTP цель отказана второй раз и настройка не
    меняется; прямая запись `serve_expose_consent` отказана; `serve_state`
    показывает метки и скоупы, но ни токенов, ни кодов; отзыв ключа называет
    «ключа нет» причиной;
  * клиент (`ui_src/app.js`): зеркало оконных целей совпадает со `scopes`,
    выбор режима наружу спрашивает диалог согласия ДО сохранения, `POST /pair`
    идёт на свой адрес позиционным телом, состояние доступа не выдумывается,
    превью отказывает честно; подписи покрыты словарём на обоих языках;
  * CSS: новых `@keyframes` нет, блок кода читает токены §2.7.
"""

import http.client
import json
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app import i18n
from app import settings_schema
from app.http import audit as serve_audit
from app.http import bind as serve_bind
from app.http import context as serve_context
from app.http import pair as serve_pair
from app.http import scopes as serve_scopes
from app.http import server as serve_http
from app.http import tokens as serve_tokens
from app.http import tls as serve_tls
from app.mesh import channel as channel_mod
from app.mesh import identity
from app.mesh import sas_words
from tests.test_gui import GuiCase
from tests.test_serve import FakeApi, ServeCase

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "ui_src" / "app.js"
CSS = ROOT / "ui_src" / "app.css"
DOC = ROOT / "docs" / "SERVER-CLIENT.md"


def _clean(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"^\s*//.*$", "", text, flags=re.M)
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)


def js_text() -> str:
    return JS.read_text(encoding="utf-8")


def js_map(name: str) -> dict:
    match = re.search(rf"var {name} = \{{(.*?)\}};", js_text(), re.S)
    assert match, f"{name} пропал из ui_src/app.js"
    return dict(re.findall(r'([A-Za-z0-9_]+):\s*"([a-z_.]+)"', match.group(1)))


def js_list(name: str) -> list:
    match = re.search(rf"var {name} = \[(.*?)\];", js_text(), re.S)
    assert match, f"{name} пропал из ui_src/app.js"
    return re.findall(r'"([A-Za-z0-9_]+)"', match.group(1))


def js_function(js: str, name: str) -> str:
    match = re.search(rf"function {name}\([\s\S]*?\n  \}}", js)
    assert match, f"{name} пропал из ui_src/app.js"
    return match.group(0)


# --------------------------------------------------------------------------- #
#  режимы привязки (app/http/bind.py)
# --------------------------------------------------------------------------- #


class TestBindModes(unittest.TestCase):
    """Выбор адреса решается согласием, а не «случайным словом в настройке»."""

    def test_mode_aliases_and_unknown_words_stay_loopback(self):
        self.assertEqual(serve_bind.normalize_mode("0.0.0.0"), "wildcard")
        self.assertEqual(serve_bind.normalize_mode("LOCAL-NETWORK"), "lan")
        self.assertEqual(serve_bind.normalize_mode("TS"), "tailscale")
        self.assertEqual(serve_bind.normalize_mode("что-то-непонятное"), "loopback")
        self.assertEqual(serve_bind.normalize_mode(None), "loopback")

    def test_outward_without_consent_stays_local_and_names_the_reason(self):
        for mode in ("lan", "tailscale", "wildcard"):
            got = serve_bind.resolve(mode, False, "", lambda: ["192.168.1.5"])
            self.assertEqual(got["address"], serve_bind.LOOPBACK,
                             f"{mode}: без согласия наружу слушать нельзя")
            self.assertIn("согласия", got["error"],
                          f"{mode}: отказ должен назвать согласие причиной")
            self.assertEqual(got["note"], "")

    def test_loopback_never_needs_consent(self):
        got = serve_bind.resolve("loopback", False, "")
        self.assertEqual(got["address"], serve_bind.LOOPBACK)
        self.assertEqual(got["error"], "")
        self.assertIn("наружу ничего не уходит", got["note"])

    def test_lan_address_picked_and_apipa_ignored(self):
        got = serve_bind.resolve("lan", True, "",
                                 lambda: ["169.254.1.9", "192.168.1.5"])
        self.assertEqual(got["address"], "192.168.1.5",
                         "адрес без DHCP не читается как сеть машины")
        self.assertIn("192.168.1.5", got["note"])
        self.assertEqual(got["error"], "")

    def test_lan_without_interface_is_named_not_silently_local(self):
        got = serve_bind.resolve("lan", True, "",
                                 lambda: ["127.0.0.1", "169.254.2.2"])
        self.assertEqual(got["address"], serve_bind.LOOPBACK)
        self.assertIn("не найден", got["error"])

    def test_tailscale_address_from_cgnat(self):
        got = serve_bind.resolve("tailscale", True, "",
                                 lambda: ["192.168.1.5", "100.64.1.2"])
        self.assertEqual(got["address"], "100.64.1.2",
                         "Tailscale раздаёт адреса из CGNAT-диапазона")
        self.assertEqual(got["error"], "")
        missing = serve_bind.resolve("tailscale", True, "", lambda: ["192.168.1.5"])
        self.assertEqual(missing["address"], serve_bind.LOOPBACK)
        self.assertIn("100.64.0.0/10", missing["error"],
                      "отказ называет диапазон, а не «адреса нет» без объяснения")

    def test_plain_port_needs_a_nonempty_allowlist(self):
        got = serve_bind.resolve("wildcard", True, "", lambda: ["192.168.1.5"])
        self.assertEqual(got["address"], serve_bind.LOOPBACK)
        self.assertIn("allowlist", got["error"])
        opened = serve_bind.resolve("wildcard", True, "192.168.1.0/24, 10.0.0.7",
                                    lambda: ["192.168.1.5"])
        self.assertEqual(opened["address"], serve_bind.WILDCARD)
        self.assertIn("2 сетей", opened["note"],
                      "пояснение говорит, сколько сетей принимает порт")

    def test_broken_allowlist_is_named_as_one_entry(self):
        got = serve_bind.resolve("wildcard", True, "192.168.1.5, chudo-adres",
                                 lambda: ["192.168.1.5"])
        self.assertEqual(got["address"], serve_bind.LOOPBACK)
        self.assertIn("chudo-adres", got["error"],
                      "битый элемент списка назван именем, а не «список битый»")
        self.assertIn("неверный адрес в allowlist", got["error"])

    def test_parse_allowlist_accepts_addresses_and_cidr(self):
        nets = serve_bind.parse_allowlist("10.0.0.7 192.168.1.0/24,100.64.3.4/32")
        self.assertEqual(len(nets), 3)
        self.assertTrue(all(net.version == 4 for net in nets))
        with self.assertRaises(ValueError):
            serve_bind.parse_allowlist("192.168.1.0/33")

    def test_allowed_loopback_is_always_local_and_others_follow_the_list(self):
        nets = serve_bind.parse_allowlist("192.168.1.0/24")
        self.assertTrue(serve_bind.allowed("127.0.0.1", nets),
                        "слушатель на этом компьютере не «чужой»")
        self.assertTrue(serve_bind.allowed("::1", nets))
        self.assertTrue(serve_bind.allowed("192.168.1.44", nets))
        self.assertFalse(serve_bind.allowed("192.168.2.44", nets))
        self.assertFalse(serve_bind.allowed("192.168.1.44", []),
                         "пустого списка хватает, чтобы принять чужой адрес")
        self.assertFalse(serve_bind.allowed("ne-ip", nets))


# --------------------------------------------------------------------------- #
#  коды привязки (app/http/pair.py)
# --------------------------------------------------------------------------- #


class TestPairing(unittest.TestCase):
    """Код - право, показанное одному клиенту один раз: срок, одноразовость, тормоз."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "serve_tokens.bin"
        self.notes: list[str] = []
        self.store = serve_tokens.TokenStore(self.path)
        self.pairing = serve_pair.Pairing(self.store, log=self.notes.append)

    def test_presets_are_the_documented_rights_without_native_scope(self):
        self.assertEqual(serve_pair.preset_scopes("viewer"), ("read", "watch"))
        self.assertEqual(serve_pair.preset_scopes("editor"),
                         ("read", "watch", "queue", "mark"))
        operator = serve_pair.preset_scopes("operator")
        self.assertEqual(set(operator), set(serve_scopes.SCOPES) - {"desktop"},
                         "оператор выдаёт право на нативные цели, которых по HTTP нет")
        with self.assertRaises(ValueError):
            serve_pair.preset_scopes("administrator")
        with self.assertRaises(ValueError):
            serve_pair.render_code("ne-format", b"x" * 32)

    def test_code_render_is_digits_or_words_from_the_same_list(self):
        digest = b"\x12\x34\x56\x78\x9a\xbc\xde\xf0"
        digits = serve_pair.render_code("digits", digest)
        self.assertRegex(digits, r"^\d{6}$")
        words = serve_pair.render_code("words", digest).split()
        self.assertEqual(len(words), 4, "4 слова - по 10 бит из первых 40 бит digest")
        self.assertTrue(all(word in sas_words.SAS_WORDS for word in words),
                        "слова берутся из того же списка, что mesh SAS")
        self.assertEqual(serve_pair.render_code("digits", digest),
                         serve_pair.render_code("digits", digest),
                         "рендер кода детерминирован: один digest - один код")

    def test_begin_shows_code_and_logs_without_its_value(self):
        shown = self.pairing.begin("viewer", minutes=3)
        self.assertEqual(shown["scopes"], ["read", "watch"])
        self.assertEqual(shown["format"], "digits")
        self.assertTrue(shown["code"])
        blob = "\n".join(self.notes)
        self.assertIn("Привязка: код показан", blob)
        self.assertNotIn(shown["code"], blob,
                         "значение кода попало в журнал: журнал читает любой")
        state = self.pairing.state()
        self.assertEqual(state["active"], 1)
        self.assertNotIn(shown["code"], json.dumps(state, ensure_ascii=False),
                         "состояние кодов отдаёт значение - код стал бы публичным")

    def test_code_is_single_use_and_a_second_client_is_refused(self):
        shown = self.pairing.begin("viewer")
        first = self.pairing.consume(shown["code"], "127.0.0.1")
        self.assertTrue(first.get("ok"), first)
        self.assertEqual(first["scopes"], ["read", "watch"])
        second = self.pairing.consume(shown["code"], "127.0.0.2")
        self.assertEqual(second.get("code"), "pairing")
        self.assertIn(i18n.t("http.pair_used", "ru"), second.get("error", ""),
                      "повтор снятого кода отказан «не принят» - это молчание "
                      "о том, что право уже выдано")
        self.assertEqual(self.pairing.state()["active"], 0)
        self.assertNotIn(shown["code"], "\n".join(self.notes))

    def test_expired_code_names_that_no_code_is_active(self):
        shown = self.pairing.begin("viewer", minutes=0.2)
        self.pairing._codes[0]["expires"] = time.time() - 1
        got = self.pairing.consume(shown["code"], "127.0.0.1")
        self.assertIn(i18n.t("http.pair_no_code", "ru"), got.get("error", ""),
                      "истёкший код = кода уже нет: причина названа явно")
        self.assertEqual(got.get("code"), "pairing")

    def test_empty_code_is_named(self):
        self.pairing.begin("viewer")
        for value in ("", None, "   "):
            got = self.pairing.consume(value, "127.0.0.1")
            self.assertIn(i18n.t("http.pair_empty", "ru"), got.get("error", ""))

    def test_whitespace_and_case_do_not_change_the_code(self):
        shown = self.pairing.begin("viewer")
        got = self.pairing.consume("  " + shown["code"].upper() + " ", "127.0.0.1")
        self.assertTrue(got.get("ok"), got)

    def test_brute_force_is_throttled_and_the_delay_grows(self):
        self.pairing.begin("viewer")
        for index in range(serve_pair.MAX_ATTEMPTS - 1):
            got = self.pairing.consume(f"{index:06d}", "203.0.113.9")
            self.assertEqual(got.get("code"), "pairing")
        self.assertEqual(self.pairing.delay_for("203.0.113.9"), 0.0,
                         "задержка включается только после лимита попыток")
        self.pairing.consume("999999", "203.0.113.9")      # лимит пройден
        self.assertEqual(self.pairing.delay_for("203.0.113.9"), serve_pair.BASE_DELAY)
        self.assertGreater(self.pairing.blocked("203.0.113.9"), 0,
                           "перебор не затормозен - право перебирают молча")
        refused = self.pairing.consume("111111", "203.0.113.9")
        self.assertEqual(refused.get("code"), "throttle")
        self.assertGreater(refused.get("retry_in"), 0,
                           "отказ не говорит, сколько ждать")
        self.assertIn(i18n.t("http.pair_throttle", "ru"), refused.get("error", ""))
        blob = "\n".join(self.notes)
        self.assertIn("перебор затормозен", blob)
        self.assertIn("неверных попыток", blob)
        self.pairing._attempts["203.0.113.9"] = serve_pair.MAX_ATTEMPTS + 20
        self.assertEqual(self.pairing.delay_for("203.0.113.9"), serve_pair.MAX_DELAY,
                         "задержка обязана иметь потолок")
        self.assertEqual(self.pairing.attempts("127.0.0.1"), 0,
                         "тормоз считается по адресу, а не по всем клиентам")
        self.assertGreaterEqual(self.pairing.attempt_total(), 6)
        self.assertNotIn("203.0.113.9", json.dumps(self.pairing.state()))

    def test_token_limit_refusal_names_the_store(self):
        limited = serve_tokens.TokenStore(self.path, max_tokens=1)
        pairing = serve_pair.Pairing(limited, log=self.notes.append)
        shown = pairing.begin("viewer")
        self.assertTrue(pairing.consume(shown["code"], "127.0.0.1").get("ok"))
        again = pairing.begin("viewer")
        refused = pairing.consume(again["code"], "127.0.0.2")
        self.assertEqual(refused.get("code"), "tokens")
        self.assertIn(i18n.t("http.tokens_limit", "ru"), refused.get("error", ""),
                      "лимит ключей назван причиной, а не «кода нет»")

    def test_issued_key_is_revocable_by_label_and_no_value_leaks(self):
        shown = self.pairing.begin("editor")
        issued = self.pairing.consume(shown["code"], "127.0.0.1")
        listed = self.store.list()
        self.assertEqual(len(listed), 1)
        self.assertNotIn(issued["token"], json.dumps(listed, ensure_ascii=False),
                         "значение токена ушло в список ключей")
        self.assertTrue(self.store.revoke(listed[0]["id"]))
        self.assertEqual(self.store.list(), [])


# --------------------------------------------------------------------------- #
#  слушатель: /pair и адресный гейт
# --------------------------------------------------------------------------- #


class TestServePairing(ServeCase):
    """Тот же слушатель, что у окна: привязка без ключа, гейт по адресу."""

    def setUp(self):
        super().setUp()
        self.server.stop()
        self.notes.clear()      # наследуемые проверки читают журнал этого слушателя
        self.pairing = serve_pair.Pairing(self.store, log=self.notes.append)
        self.server = serve_http.ServeServer(
            FakeApi(), bind="127.0.0.1", port=0, rate_bps=0, watchers_max=0,
            tokens=self.store, audit=self.journal, language=lambda: "ru",
            log=self.notes.append, pairing=self.pairing, mode="loopback")
        self.port = self.server.start()
        self.addCleanup(self.server.stop)

    def shown_code(self, preset: str = "viewer") -> str:
        return self.pairing.begin(preset)["code"]

    def extra_server(self, **kwargs):
        params = {"bind": "127.0.0.1", "port": 0, "tokens": self.store,
                  "audit": self.journal, "language": lambda: "ru",
                  "log": self.notes.append}
        params.update(kwargs)
        server = serve_http.ServeServer(FakeApi(), **params)
        self.addCleanup(server.stop)
        return server

    def post_to(self, port: int, path: str, body) -> tuple[int, dict]:
        conn = http.client.HTTPSConnection("127.0.0.1", port, timeout=10,
                                           context=self.ctx)
        try:
            conn.request("POST", path,
                         body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                         headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))
        finally:
            conn.close()

    # -- выдача ключа --------------------------------------------------------

    def test_pair_issues_a_key_that_reads_its_own_rights(self):
        code = self.shown_code()
        status, payload, text, _ = self.request("POST", "/pair",
                                                body=[{"code": code}])
        self.assertEqual(status, 200, payload or text[:80])
        self.assertTrue(payload.get("ok"))
        self.assertTrue(payload.get("token"))
        self.assertEqual(payload.get("scopes"), ["read", "watch"])
        self.assertEqual(payload.get("label"), "клиент read+watch")
        status, whoami, _, _ = self.request("GET", "/whoami",
                                            token=payload["token"])
        self.assertEqual(status, 200, whoami)
        self.assertEqual(whoami.get("scopes"), ["read", "watch"])
        self.assertEqual(text.count(payload["token"]), 1,
                         "значение токена вернулось в ответе дважды")
        blob = "\n".join(self.journal.tail())
        self.assertIn("pair", blob, "привязка не видна в журнале аудита")
        self.assertNotIn(code, blob, "значение кода привязки попало в журнал")
        self.assertNotIn(payload["token"], blob, "значение токена попало в журнал")
        self.assertIn("ключ выдан", "\n".join(self.notes))

    def test_pair_accepts_the_kwargs_shape_too(self):
        code = self.shown_code()
        status, payload, _, _ = self.request("POST", "/pair", body={"code": code})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload.get("ok"))

    def test_pair_needs_no_bearer_key(self):
        # Привязка - это и есть получение ключа: Authorization здесь не нужен.
        code = self.shown_code()
        status, payload, _, _ = self.request("POST", "/pair", body=[{"code": code}])
        self.assertEqual(status, 200, payload)

    def test_wrong_code_is_401_with_a_named_reason(self):
        self.shown_code()
        status, payload, _, _ = self.request("POST", "/pair",
                                             body=[{"code": "000000"}])
        self.assertEqual(status, 401)
        self.assertEqual(payload.get("code"), "pairing")
        self.assertIn(i18n.t("http.pair_denied", "ru"), payload.get("error", ""))

    def test_no_code_shown_names_the_window(self):
        status, payload, _, _ = self.request("POST", "/pair",
                                             body=[{"code": "123456"}])
        self.assertEqual(status, 401)
        self.assertIn(i18n.t("http.pair_no_code", "ru"), payload.get("error", ""),
                      "сервер не показывал код - клиент читает это как причину")

    def test_empty_code_is_named(self):
        self.shown_code()
        status, payload, _, _ = self.request("POST", "/pair", body=[{"code": ""}])
        self.assertEqual(status, 401)
        self.assertIn(i18n.t("http.pair_empty", "ru"), payload.get("error", ""))

    def test_throttle_returns_429_and_retry_in(self):
        self.shown_code()
        for index in range(serve_pair.MAX_ATTEMPTS):
            status, payload, _, _ = self.request("POST", "/pair",
                                                 body=[{"code": f"{index + 1:06d}"}])
            self.assertEqual(status, 401, payload)
        status, payload, _, _ = self.request("POST", "/pair",
                                             body=[{"code": "999999"}])
        self.assertEqual(status, 429)
        self.assertEqual(payload.get("code"), "throttle")
        self.assertGreater(payload.get("retry_in"), 0,
                           "задержка названа, но не сказано, сколько ждать")
        self.assertIn(i18n.t("http.pair_throttle", "ru"), payload.get("error", ""))
        blob = "\n".join(self.journal.tail())
        self.assertIn("перебор", blob, "тормоз перебора не виден в журнале аудита")
        self.assertIn("429", blob)

    def test_pair_is_post_only(self):
        status, payload, _, _ = self.request("GET", "/pair")
        self.assertEqual(status, 405)
        self.assertIn(i18n.t("http.pair_post_only", "ru"), payload.get("error", ""))

    def test_broken_body_is_400(self):
        status, payload, _, _ = self.request("POST", "/pair", raw="ne json")
        self.assertEqual(status, 400)
        self.assertEqual(payload.get("code"), "bad_json")

    def test_without_pairing_the_route_refuses_with_a_reason(self):
        """Слушатель без реестра кодов: отказ назван, а не «тихий успех»."""
        bare = self.extra_server()
        port = bare.start()
        status, payload = self.post_to(port, "/pair", [{"code": "123456"}])
        self.assertEqual(status, 409)
        self.assertIn(i18n.t("http.pair_no_code", "ru"), payload.get("error", ""),
                      "привязка не настроена - клиент читает причину, а не пустоту")

    # -- адресный гейт -------------------------------------------------------

    def test_local_address_is_served(self):
        status, _, text, _ = self.request("GET", "/")
        self.assertEqual(status, 200, text[:80])
        self.assertIn("OMNISTASH_HTTP", text)

    def test_address_outside_the_allowlist_is_refused_before_anything_is_served(self):
        """Гейт по адресу (§7): «кто-то подключился» не равно «доступ есть»."""
        with mock.patch.object(serve_http.bind_rule, "allowed",
                               lambda addr, nets: False):
            gated = self.extra_server(allow_text="192.168.1.0/24", mode="wildcard")
            port = gated.start()
            conn = http.client.HTTPSConnection("127.0.0.1", port, timeout=10,
                                               context=self.ctx)
            try:
                conn.request("GET", "/")
                response = conn.getresponse()
                payload = json.loads(response.read().decode("utf-8"))
            finally:
                conn.close()
        self.assertEqual(response.status, 403)
        self.assertEqual(payload.get("code"), "allowlist")
        self.assertIn(i18n.t("http.allowlist", "ru"), payload.get("error", ""),
                      "отказ по адресу назван, а не молчание")
        blob = "\n".join(self.journal.tail())
        self.assertIn("connect", blob, "отказ по адресу не виден в журнале аудита")

    def test_plain_port_without_allowlist_does_not_start(self):
        server = self.extra_server(mode="wildcard", allow_text="", bind="0.0.0.0")
        with self.assertRaises(serve_http.ServeError) as caught:
            server.start()
        self.assertIn(i18n.t("http.allowlist_required", "ru"), str(caught.exception))
        self.assertFalse(server.running, "отказ не должен числиться «поднят»")

    def test_broken_allowlist_does_not_start(self):
        server = self.extra_server(mode="wildcard", allow_text="192.168.1.0/33",
                                   bind="0.0.0.0")
        with self.assertRaises(serve_http.ServeError) as caught:
            server.start()
        self.assertIn("неверный адрес в allowlist", str(caught.exception))

    def test_exposure_and_pairing_are_written_to_the_journal_at_start(self):
        log = "\n".join(self.notes)
        self.assertIn("Экспозиция: loopback", log)
        self.assertIn("якорь доверия", log,
                      "журнал не говорит, что якорь доверия - код привязки")
        self.assertIn("код показывает окно сервера", log)

    def test_drop_video_still_works_after_the_rebuild(self):
        """Слушатель с привязкой не теряет зрителей: drop_video тот же."""
        self.server.drop_video(42)
        self.assertEqual(self.server.mode, "loopback")


# Наследуемые проверки S1/S2 (страница, TLS, байтовые маршруты, тело запроса)
# живут в tests/test_serve.py на обычном слушателе: здесь слушатель собран с
# привязкой, и повторный прогон тех же проверок удваивал бы время набора без
# нового покрытия.
for _inherited in [name for name in dir(ServeCase) if name.startswith("test_")]:
    if _inherited not in vars(TestServePairing):
        setattr(TestServePairing, _inherited, None)


# --------------------------------------------------------------------------- #
#  фасад Api: окно против HTTP (app/http/context.py)
# --------------------------------------------------------------------------- #


class TestServeExposureInGui(GuiCase):
    """Согласие и значение кода решает человек на сервере, не удалённые руки."""

    def setUp(self):
        super().setUp()
        # Профиль HTTP (ключи, журнал аудита, TLS-идентичность) переносим в
        # папку теста: иначе serve_* писали бы в настоящий профиль человека.
        for module in (identity, channel_mod, serve_tls):
            patcher = mock.patch.object(module, "profile_dir", lambda: self.dir)
            patcher.start()
            self.addCleanup(patcher.stop)
        identity.reset_cache()
        self.addCleanup(identity.reset_cache)
        channel_mod.reset_cache()
        self.addCleanup(channel_mod.reset_cache)
        for module, name, path in ((serve_tokens, "serve_tokens_path",
                                    self.dir / "serve_tokens.bin"),
                                   (serve_audit, "serve_audit_path",
                                    self.dir / "serve_audit.log")):
            patcher = mock.patch.object(module, name, lambda path=path: path)
            patcher.start()
            self.addCleanup(patcher.stop)
        # TEST-NET-1: адрес, которого нет на этой машине - LAN «не найден» не
        # должен быть правдой, а подъём слушателя на нём обязан отказаться.
        patcher = mock.patch.object(serve_bind, "read_addresses",
                                    lambda: ["192.0.2.1"])
        patcher.start()
        self.addCleanup(patcher.stop)

    def listener(self, api):
        """Поднять слушатель на системном порту (не 1001: демо и тесты не мешают)."""
        api._serve_server._port = 0
        api._serve_server.start()
        self.addCleanup(api._serve_server.stop)
        return api._serve_server

    def journal(self, api) -> str:
        return "\n".join(api._logs)

    def test_outward_mode_without_consent_stays_local_with_a_journal_line(self):
        api = self.make_api()
        api.save_setting({"key": "serve_bind", "value": "lan"})
        self.assertEqual(api._serve_bind, "127.0.0.1",
                         "режим lan без согласия привязал бы слушатель наружу")
        self.assertIn("согласия", self.journal(api),
                      "отказ без согласия не назван в журнале программы")
        state = api.serve_state()
        self.assertEqual(state["mode"], "lan",
                         "состояние показывает выбранный режим, а не замолчанный")
        self.assertFalse(state["consent"])
        self.assertTrue(state["problem"],
                        "проблема режима читается подписью, а не пустотой")
        self.assertFalse(state["running"])

    def test_consent_from_window_moves_the_listener_to_the_mode_address(self):
        api = self.make_api()
        api.serve_consent_accept({"accept": True})
        api.save_setting({"key": "serve_bind", "value": "lan"})
        self.assertEqual(api._serve_bind, "192.0.2.1",
                         "после согласия адрес выбирается по режиму LAN")
        self.assertFalse(api._serve_server.running,
                         "адрес не местный: слушатель не поднят и не должен "
                         "числиться поднятым")

    def test_unbindable_address_is_named_in_the_journal(self):
        api = self.make_api()
        api.save_setting({"key": "serve_enabled", "value": True})
        api.serve_consent_accept({"accept": True})
        api.save_setting({"key": "serve_bind", "value": "lan"})
        blob = self.journal(api)
        self.assertIn("не поднят", blob,
                      "отказ подъёма назван в журнале причиной, а не молчанием")
        self.assertIn("Экспозиция", blob)
        self.assertFalse(api._serve_server.running)

    def test_declining_consent_puts_the_listener_back_to_loopback(self):
        api = self.make_api()
        api.serve_consent_accept({"accept": True})
        api.save_setting({"key": "serve_bind", "value": "lan"})
        self.assertEqual(api._serve_bind, "192.0.2.1")
        api.serve_consent_accept({"accept": False})
        self.assertEqual(api._serve_bind, "127.0.0.1",
                         "отзыв согласия обязан вернуть слушатель на loopback")
        self.assertFalse(api._current_settings().get("serve_expose_consent"))

    def test_consent_written_directly_is_refused(self):
        api = self.make_api()
        got = api.save_setting({"key": "serve_expose_consent", "value": True})
        self.assertIn(i18n.t("http.expose_consent", "ru"), got.get("error", ""),
                      "прямая запись согласия обязана называть причину отказа")
        self.assertFalse(api._current_settings().get("serve_expose_consent"))
        self.assertIn("не сохранено", self.journal(api))

    def test_pair_code_and_consent_are_refused_over_http(self):
        """HTTP-вызов той же цели: отказ подписью, действие не выполняется."""
        api = self.make_api()
        self.listener(api)
        with serve_context.http_call():
            code = api.serve_pair_begin({})
            consent = api.serve_consent_accept({"accept": True})
        self.assertEqual(code.get("code"), "window")
        self.assertIn(i18n.t("http.pair_code_local", "ru"), code.get("error", ""))
        self.assertEqual(consent.get("code"), "window")
        self.assertIn(i18n.t("http.expose_consent", "ru"), consent.get("error", ""))
        self.assertFalse(api._current_settings().get("serve_expose_consent"),
                         "согласие по HTTP принято - настройка изменилась молча")
        self.assertEqual(api.serve_pair_status()["active"], 0,
                         "по HTTP код был создан, хотя его значение не отдано")

    def test_window_can_show_a_code_and_the_state_names_it_as_active(self):
        api = self.make_api()
        self.listener(api)
        shown = api.serve_pair_begin({"format": "words"})
        self.assertTrue(shown.get("ok"), shown)
        self.assertEqual(shown["format"], "words")
        self.assertEqual(len(shown["code"].split()), 4)
        status = api.serve_pair_status()
        self.assertEqual(status["active"], 1)
        self.assertNotIn(shown["code"], json.dumps(status, ensure_ascii=False),
                         "статус кодов отдаёт значение кода")
        self.assertNotIn(shown["code"], self.journal(api),
                         "значение кода попало в журнал программы")

    def test_pair_begin_without_the_listener_names_the_listener(self):
        api = self.make_api()
        got = api.serve_pair_begin({})
        self.assertEqual(got.get("code"), "pairing")
        self.assertIn(i18n.t("http.pair_listener_down", "ru"), got.get("error", ""))

    def test_broken_preset_is_named_by_begin(self):
        api = self.make_api()
        self.listener(api)
        api.save_setting({"key": "serve_pair_scope", "value": "viewer"})
        api._settings_cache["serve_pair_scope"] = "chudo"   # мимо схемы: битое значение
        got = api.serve_pair_begin({})
        self.assertIn("неизвестный пресет", got.get("error", ""),
                      "битый пресет прав отказан подписью, а не молча")

    def test_state_shows_labels_and_scopes_but_never_token_values(self):
        api = self.make_api()
        server = self.listener(api)
        shown = api.serve_pair_begin({})
        issued = server.pairing.consume(shown["code"], "127.0.0.1")
        state = api.serve_state()
        self.assertEqual(state["count"], 1)
        self.assertEqual(state["tokens"][0]["scopes"], ["read", "watch"])
        blob = json.dumps(state, ensure_ascii=False)
        self.assertNotIn(issued["token"], blob, "значение токена ушло в снимок")
        self.assertNotIn(shown["code"], blob, "значение кода ушло в снимок")
        self.assertEqual(state["mode"], "loopback")
        self.assertTrue(state["url"].startswith("https://127.0.0.1:"))
        self.assertEqual(state["pair_scope"], "viewer")

    def test_http_can_read_state_and_revoke_a_key(self):
        api = self.make_api()
        server = self.listener(api)
        shown = api.serve_pair_begin({})
        issued = server.pairing.consume(shown["code"], "127.0.0.1")
        with serve_context.http_call():
            state = api.serve_state()
            revoke = api.serve_token_revoke({"id": state["tokens"][0]["id"]})
        self.assertTrue(revoke.get("ok"), revoke)
        blob = json.dumps(state, ensure_ascii=False)
        self.assertNotIn(issued["token"], blob)
        self.assertNotIn(shown["code"], blob)

    def test_revoke_names_a_missing_key_as_a_reason(self):
        api = self.make_api()
        server = self.listener(api)
        shown = api.serve_pair_begin({})
        server.pairing.consume(shown["code"], "127.0.0.1")
        listed = api.serve_state()["tokens"]
        self.assertTrue(api.serve_token_revoke({"id": listed[0]["id"]}).get("ok"))
        self.assertEqual(api.serve_state()["count"], 0)
        again = api.serve_token_revoke({"id": listed[0]["id"]})
        self.assertIn(i18n.t("http.tokens_absent", "ru"), again.get("error", ""),
                      "повторный отзыв читался бы как «отозван»")
        self.assertFalse(api.serve_token_revoke({}).get("ok"))

    def test_schema_fields_are_the_documented_modes(self):
        self.assertEqual([pair[0] for pair in
                          settings_schema.field("serve_bind")["choices"]],
                         ["loopback", "lan", "tailscale", "wildcard"])
        consent = settings_schema.field("serve_expose_consent")
        self.assertTrue(consent["hidden"],
                        "согласие не должно быть флажком в карточке: решает диалог")
        self.assertEqual(settings_schema.field("serve_allowlist")["type"], "str")
        self.assertEqual(settings_schema.field("serve_pair_scope")["default"], "viewer",
                         "клиент по умолчанию не получает право качать")
        self.assertEqual(settings_schema.field("serve_pair_minutes")["default"], 3)
        widget = settings_schema.field("_serve_access")
        self.assertEqual(widget["type"], "serve")
        self.assertTrue(widget["transient"],
                        "виджет не должен писаться в settings.json")


# --------------------------------------------------------------------------- #
#  клиентская проводка (ui_src/app.js, app.css)
# --------------------------------------------------------------------------- #

# Подписи S6: обязан быть вариант на обоих языках.
CAPTIONS = [
    "serve.refresh", "serve.pending", "http.no_server", "http.pair_empty",
    "http.pair_throttle", "http.pair_code_local", "http.pair_listener_down",
    "http.expose_consent", "http.tokens_revoked", "http.tokens_absent",
    "http.pair_denied", "http.pair_no_code", "http.pair_expired",
    "http.pair_post_only", "http.allowlist", "http.allowlist_required",
    "http.tokens_limit", "pair.store_none", "tokens.store_none",
    "pair.running", "pair.down", "pair.mode", "pair.address",
    "pair.consent_on", "pair.consent_off", "pair.attempts", "pair.code.active",
    "pair.code.none", "pair.code.expires", "pair.code.words", "pair.code.digits",
    "pair.allow_none", "pair.title", "pair.code.label", "pair.code.ph",
    "pair.begin", "pair.submit", "pair.note", "pair.pairing", "pair.keys",
    "pair.keys_none", "pair.revoke", "expose.title", "expose.text",
    "expose.check", "expose.denied", "expose.accept", "expose.decline",
    "expose.dpi", "settings.serve_allowlist.label",
    "settings.serve_pair_scope.label", "settings.serve_pair_minutes.label",
    "settings._serve_access.label", "settings._serve_access.hint",
]


class TestClientExposure(unittest.TestCase):
    def setUp(self):
        self.js = _clean(js_text())

    def test_window_only_targets_are_mirrored(self):
        self.assertEqual(set(js_list("HTTP_WINDOW_ONLY")), serve_scopes.WINDOW_ONLY,
                         "список «только окно» в клиенте разошёлся со scopes.py")
        self.assertEqual(js_map("HTTP_WINDOW_KEYS"), serve_scopes.WINDOW_REASON,
                         "подпись отказа оконной цели разошлась со словарём")
        for key in serve_scopes.WINDOW_REASON.values():
            self.assertTrue(i18n.t(key, "en"), f"{key}: нет английской подписи")
        self.assertTrue(set(serve_scopes.WINDOW_ONLY) <= set(serve_scopes.SCOPE_OF),
                        "оконная цель не описана в таблице скоупов")

    def test_language_switch_restates_the_rights_line(self):
        sync = js_function(self.js, "syncLanguage")
        self.assertIn("clientLanguage()", sync,
                      "клиент читает подписи своим локальным языком (§8)")
        self.assertLess(sync.index("applyLanguage()"), sync.index("accessBand("),
                        "строка прав осталась на прежнем языке после смены языка клиента")
        self.assertIn("applyAccess()", sync,
                      "подписи недоступных целей не пересказаны на новом языке")

    def test_revoked_key_names_the_reason_and_restores_the_entry(self):
        post = js_function(self.js, "httpPost")
        branch = re.search(r"res\.status === 401[\s\S]*?\n        \}", post)
        self.assertTrue(branch, "401 не разбирается как отказ ключа")
        body = branch.group(0)
        self.assertIn("httpProblem.unpaired = true", body)
        self.assertIn("state.scopes = null", body,
                      "ключ отозван, а строка прав осталась прошлой - это враньё")
        self.assertIn("accessBand(", body,
                      "отзыв ключа не возвращает поле кода и причину в строке прав")

    def test_window_only_call_is_not_sent(self):
        call = js_function(self.js, "call")
        self.assertLess(call.index("accessReason"), call.index("bridge()"),
                        "оконная цель всё равно уходит по HTTP")
        self.assertIn('"window"', call, "код отказа оконной цели теряется")
        reason = js_function(self.js, "accessReason")
        self.assertIn("HTTP_WINDOW_KEYS", reason)

    def test_outward_mode_asks_for_consent_before_saving(self):
        handler = re.search(r'key === "serve_bind"[\s\S]*?saveSetting\(key, value\)',
                            self.js)
        self.assertTrue(handler, "выбор режима наружу снова сохраняет без согласия")
        body = handler.group(0)
        self.assertLess(body.index("exposeDialog"), body.index("saveSetting"),
                        "согласие спрашивается после сохранения - галка задним числом")
        self.assertIn("input.value = previous", body,
                      "отказ обязан вернуть select на прежнее значение")

    def test_consent_dialog_names_what_leaves_and_the_dpi_zone(self):
        dialog = js_function(self.js, "exposeDialog")
        self.assertIn('t("expose.text"', dialog)
        self.assertIn('t("expose.dpi"', dialog)
        self.assertIn("legalDialog", dialog,
                      "согласие без галки и отказа - это confirm без причины")

    def test_pairing_goes_to_the_own_address_positionally(self):
        pair = js_function(self.js, "pairThisBrowser")
        self.assertIn('base + "/pair"', pair)
        self.assertIn('JSON.stringify([{ code: code }])', pair)
        self.assertIn('credentials: "same-origin"', pair)
        self.assertIn('localStorage.setItem("omnistash.token"', pair)
        self.assertIn("fetchAccess()", pair,
                      "ключ принят, а права перечитаны не были - band остался бы старым")
        self.assertIn("loadInitial()", pair,
                      "ключ принят, а карточка не перечитана: до привязки "
                      "get_initial был отказан и настроек не видно")
        self.assertIn("http.pair_throttle", pair, "задержка перебора не названа")
        self.assertNotRegex(self.js, r'fetch\(\s*["\']https?://',
                            "запрос на чужой хост - supply chain")

    def test_empty_code_is_named_before_the_request(self):
        pair = js_function(self.js, "pairThisBrowser")
        self.assertLess(pair.index('t("http.pair_empty"'), pair.index("fetch("))

    def test_serve_widget_state_is_read_not_invented(self):
        widget = js_function(self.js, "serveWidget")
        self.assertIn('id="serve-state"', widget)
        self.assertIn('role="status"', widget)
        self.assertIn('aria-live="polite"', widget)
        self.assertIn('data-need="server_config"', widget)
        self.assertIn('data-window="serve_pair_begin"', widget,
                      "кнопка кода в окне не подписана как оконная цель")
        self.assertIn('id="pair-code"', widget)
        caption = js_function(self.js, "serveCaption")
        for key in ("pair.running", "pair.down", "pair.mode", "pair.consent_on",
                    "pair.attempts", "pair.keys", "pair.code.none"):
            self.assertIn(f'"{key}"', caption, f"{key}: состояние его не называет")
        self.assertIn("state.problem", caption, "причина отказа режима не читается")
        pending = js_function(self.js, "renderServeState")
        self.assertIn("serve.pending", pending,
                      "непрочитанное состояние молчит вместо подписи")
        keys = js_function(self.js, "renderServeKeys")
        self.assertIn("data-token-revoke", keys)
        self.assertIn('data-need="server_config"', keys)
        binding = js_function(self.js, "bindServeActions")
        binding = js_function(self.js, "bindServeActions")
        self.assertIn('closest("[data-token-revoke]")', binding,
                      "отзыв ключа связан на кнопки списка: после перечитания "
                      "состояния новые кнопки остались бы без обработчика")

    def test_widget_is_rendered_and_bound(self):
        self.assertIn('field.type === "serve"', self.js)
        self.assertIn("bindServeActions()", self.js)
        self.assertIn("serveWidget()", self.js)

    def test_pairing_entry_exists_without_any_right(self):
        """Поле кода вне карточки (§5): без ключа карточка не читается - вход там."""
        html = (ROOT / "ui_src" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="pair-box"', html, "в разметке нет блока привязки")
        self.assertIn('id="pair-code-out"', html, "клиенту негде ввести код")
        self.assertIn('id="pair-btn-out"', html, "нет кнопки привязки")
        self.assertIn('role="status"', html)
        box = js_function(self.js, "syncPairBox")
        self.assertIn('httpProblem.unpaired', box,
                      "блок привязки не следит за «ключа нет» - вход пропал бы")
        self.assertIn("!httpToken()", box,
                      "отозванный ключ не возвращает поле кода")
        self.assertIn("bindPairBox()", self.js, "блок привязки не привязан к событиям")

    def test_preview_mock_refuses_honestly(self):
        state = re.search(r"serve_state: function \([\s\S]*?\n      \},", self.js)
        self.assertTrue(state, "превью не показывает состояние доступа вовсе")
        self.assertIn("running: false", state.group(0))
        self.assertIn("превью", state.group(0), "превью выдумывает «слушатель поднят»")
        begin = re.search(r"serve_pair_begin: function \([\s\S]*?\n      \},", self.js)
        self.assertTrue(begin)
        self.assertIn("http.pair_listener_down", begin.group(0),
                      "превью выдаёт выдуманный код привязки")
        save = re.search(r"save_setting: function \(pair\)[\s\S]*?\n      \},", self.js)
        self.assertTrue(save)
        self.assertIn("serve_expose_consent", save.group(0),
                      "мок превью принимает согласие напрямую - бэкенд отказывает")
        revoke = re.search(r"serve_token_revoke: function \(req\)[\s\S]*?\n      \},",
                           self.js)
        self.assertTrue(revoke, "превью не даёт отзыв ключа")

    def test_captions_covered_in_both_languages_and_mock(self):
        used = set(re.findall(r't\(\s*"([a-z_.]+)"', self.js))
        block = re.search(r"var MOCK_EN\s*=\s*\{(.*?)\n    \};", self.js, re.S).group(1)
        mock_map = {}
        for match in re.finditer(r'"([a-z_.]+)":\s*((?:"[^"]*"\s*\+?\s*)+)', block):
            mock_map[match.group(1)] = "".join(re.findall(r'"([^"]*)"', match.group(2)))
        for key in sorted(set(CAPTIONS)):
            self.assertTrue(i18n.has(key), f"{key} нет в словаре")
            ru = i18n.t(key, "ru")
            en = i18n.t(key, "en")
            self.assertTrue(ru, f"{key} без русской подписи")
            self.assertTrue(en, f"{key} без английской подписи")
            self.assertFalse(any(char in "абвгдежзийклмнопрстуфхцчшщэюя"
                                 for char in en), f"{key}: английская подпись с кириллицей")
            if key.startswith("settings.") or key not in used:
                continue        # подпись карточки или текст сервера: превью её не рисует
            self.assertIn(key, mock_map, f"превью не покрывает подпись {key}")
            self.assertEqual(mock_map[key], en,
                             f"английская подпись {key} в превью ≠ словарю")

    def test_fallback_captions_match_the_dictionary(self):
        pairs = {}
        for match in re.finditer(r't\(\s*"([a-z_.]+)",\s*((?:"[^"]*"\s*\+?\s*)+)', self.js):
            pairs[match.group(1)] = "".join(re.findall(r'"([^"]*)"', match.group(2)))
        for key in sorted(set(CAPTIONS)):
            if key.startswith("settings.") or key not in pairs:
                continue
            self.assertEqual(pairs[key], i18n.t(key, "ru"),
                             f"подпись в коде ({pairs[key]}) ≠ словарю {key}")

    def test_no_certificate_pin_in_the_client(self):
        """§5: якорь доверия - код привязки; пин эфемерного сертификата не реализован."""
        self.assertNotRegex(self.js, r'"omnistash\.pin"|omnistash\.pin\s*=|SPKI',
                            "пин сертификата появился бы ложью: ключ меняется при подъёме")
        self.assertIn('"omnistash.token"', self.js)

    def test_css_budget_for_the_widget(self):
        css = _clean(CSS.read_text(encoding="utf-8"))
        names = set(re.findall(r"@keyframes\s+([A-Za-z0-9_-]+)\s*\{", css))
        self.assertEqual(names, {"pulse", "cat-sweep"}, "новый @keyframes в S6 запрещён")
        for selector in (".serve-box", ".serve-code", ".pair-box"):
            rule = re.search(r"(?m)^" + re.escape(selector) + r"\s*\{([^}]*)\}", css)
            self.assertTrue(rule, f"нет правила {selector}")
            body = rule.group(1)
            self.assertNotRegex(body, r"#[0-9a-fA-F]{3,6}",
                                f"{selector}: литеральный цвет (§2 DESIGN-UI)")
            self.assertNotIn("transition", body, f"{selector}: переход без токена (§4.2)")
            self.assertNotIn("animation", body, f"{selector}: цикл без токена")
        code = re.search(r"(?m)^\.serve-code\s*\{([^}]*)\}", css).group(1)
        for token in ("--surface-2", "--info-line", "--r-2", "--mono", "--fs-lg"):
            self.assertIn(f"var({token})", code, f"{token} не читается в блоке кода")

    def test_generated_text_has_no_cjk_or_latin_glue(self):
        cjk = re.compile(r"[\u3400-\u9fff\uac00-\udfff]")
        glue = re.compile(r"[A-Za-z][а-яё]|[а-яё][A-Za-z]")
        skip = ("http.", "pair.", "serve.", "expose.", "settings.", "tokens.",
                "data-", "aria-", "localstorage", "post", "get", "head",
                "serve_bind", "serve_pair", "serve_expose_consent",
                "serve_allowlist", "serve_state", "serve_token_revoke",
                "serve_consent_accept", "read", "watch", "queue", "mark",
                "sources", "mesh", "accounts", "server_config", "desktop",
                "allowlist", "cidr", "ip", "sha256", "dpapi", "etag", "json",
                "api", "app.js", "app.css", "i18n", "t(\"", "def ", "import ",
                "from ", "cgnat", "tailscale", "loopback", "wildcard", "lan",
                "sas", "http.server", "dpi", "zapret", "apipa", "dhcp",
                "hls", "media", "subs", "thumb", "whoami", "bearer", "scope")
        for path in (JS, CSS, ROOT / "ui_src" / "index.html",
                    ROOT / "app/http/bind.py", ROOT / "app/http/pair.py",
                    ROOT / "app/http/context.py", ROOT / "app/http/server.py",
                    ROOT / "app/http/scopes.py", ROOT / "app/http/__init__.py",
                    ROOT / "app/gui.py", ROOT / "app/i18n.py",
                    ROOT / "app/settings_schema.py", ROOT / "app/themes.py",
                    ROOT / "tests" / "test_themes.py", DOC, Path(__file__)):
            text = path.read_text(encoding="utf-8")
            hits = [f"{n}: {c}" for n, line in enumerate(text.splitlines(), 1)
                    for c in line if cjk.search(c)]
            self.assertEqual(hits, [], f"{path.name}: CJK в тексте {hits[:4]}")
            glued = []
            for match in glue.finditer(text):
                fragment = text[max(0, match.start() - 25):match.end() + 25]
                if any(token in fragment.lower() for token in skip):
                    continue
                glued.append((path.name, fragment.replace("\n", " ")))
            self.assertEqual(glued, [], f"{path.name}: склейка латиницы с кириллицей {glued[:4]}")


class TestDocContract(unittest.TestCase):
    def test_doc_names_the_pairing_route_and_the_gates(self):
        doc = DOC.read_text(encoding="utf-8")
        self.assertIn("POST /pair", doc, "§3 не называет маршрут привязки")
        self.assertIn("GET /pair", doc, "§3 не называет 405 на чтение кода")
        self.assertIn("retry_in", doc, "§3 не называет задержку перебора")
        self.assertIn("serve_pair_begin", doc, "§4 не называет оконную цель кода")
        self.assertIn("serve_consent_accept", doc, "§4 не называет оконное согласие")
        self.assertIn("serve_expose_consent", doc, "§7 не называет настройку согласия")
        self.assertIn("serve_allowlist", doc, "§7 не называет allowlist")
        self.assertIn("адрес клиента не в allowlist", doc,
                      "§7 не называет отказ по адресу")
        self.assertIn("НЕ реализован", doc,
                      "§5 должен честно сказать, что пин сертификата не реализован")
        self.assertIn("MAX_TOKENS", doc, "§5 не называет лимит числа ключей")

    def test_doc_names_the_modes_and_the_widget(self):
        doc = DOC.read_text(encoding="utf-8")
        self.assertIn("serve_pair_scope", doc, "§5 не называет пресеты прав")
        self.assertIn("serve_pair_minutes", doc, "§5 не называет срок кода")
        self.assertIn("_serve_access", doc, "§8 не называет виджет состояния доступа")
        self.assertIn("pair-code-out", doc,
                      "§8 не называет поле кода вне карточки (§5: вход без права)")
        self.assertIn("loadInitial", doc,
                      "§8 не говорит, что карточка перечитывается после привязки")
        for mode in ("loopback", "lan", "tailscale", "wildcard"):
            self.assertIn(mode, doc, f"§7 не называет режим {mode}")
        self.assertIn("100.64.0.0/10", doc, "§7 не называет диапазон Tailscale")


if __name__ == "__main__":
    unittest.main()
