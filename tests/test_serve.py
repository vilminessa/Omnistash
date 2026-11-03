"""SV-C S1: HTTPS-сервер клиента рядом с окном (docs/SERVER-CLIENT.md §3, §5).

Сервер - тот же процесс, что окно: слушатель вызывает тот же фасад `Api`, а
права решает токен со скоупами. Сеть локальная (127.0.0.1, системный порт),
профиль (токены, журнал аудита, TLS-идентичность) переносим во временную
папку - тесты не пишут в настоящий %LOCALAPPDATA%\\Omnistash.

TLS проверяется без CA: сертификат эфемерный и самоподписанный (как у
mesh-канала), решение о доверии в браузере принимает человек (§5), поэтому
тест читает рукопожатие, версию TLS и SAN, а не цепочку подписей.

Что проверяется:
  * маршрут: страница с меткой сервера, GET /api/* -> 405, чужой путь -> 404;
  * гейт: без ключа -> 401 «привязка», без скоупа -> 403 с именем скоупа,
    нативная цель -> 403 «только на сервере», локальная/внутренняя -> 404;
  * тело: позиционный список и kwargs-объект, пустое тело - ноль аргументов,
    битый JSON -> 400, большое тело -> 413 (не тихая обрезка);
  * отказ процесса остаётся подписью самого Api (200 + {error}): 409 - за
    байтовыми маршрутами (S2/S3), не за нами;
  * журнал аудита: ключ -> метод -> код -> адрес, без токенов и путей;
  * токены: DPAPI, лимит числа ключей, отзов, битый файл назван причиной;
  * скоупы: SCOPE_OF сверяется с фасадом Api, со списком клиента в app.js и
    с таблицей §4 документа;
  * wiring окна: флаг выключен - слушателя нет; включён - следуют порт,
    адрес и лимиты; отказ старта - честная строка в журнале, окно не падает.
"""

import ast
import hashlib
import http.client
import json
import re
import socket
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509 import IPAddress, SubjectAlternativeName

from app import i18n, settings_schema
from app.gui import Api
from app.http import audit as serve_audit
from app.http import scopes as serve_scopes
from app.http import server as serve_http
from app.http import tokens as serve_tokens
from app.http import tls as serve_tls
from app.mesh import channel as channel_mod
from app.mesh import firewall as mesh_firewall
from app.mesh import identity
from tests.test_gui import GuiCase

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "ui_src" / "app.js"
DOC = ROOT / "docs" / "SERVER-CLIENT.md"
BACKEND = ROOT / "app" / "gui.py"


def api_public_names() -> set:
    """Публичные методы класса Api из app/gui.py (без импорта модуля)."""
    tree = ast.parse(BACKEND.read_text(encoding="utf-8"))
    cls = [node for node in tree.body
           if isinstance(node, ast.ClassDef) and node.name == "Api"]
    assert cls, "в app/gui.py нет класса Api"
    return {node.name for node in cls[0].body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not node.name.startswith("_")}


def js_list(name: str) -> list:
    js = JS.read_text(encoding="utf-8")
    match = re.search(rf"var {name} = \[(.*?)\];", js, re.DOTALL)
    assert match, f"{name} пропал из ui_src/app.js"
    return re.findall(r'"([A-Za-z0-9_]+)"', match.group(1))


class FakeApi:
    """Фасад для проверки маршрута и гейта: те же подписи отказа, что у Api.

    Настоящий Api поднимает базу и воркеры - здесь важны только коды и тела.
    """

    def ui_texts(self):
        return {"ok": True, "language": i18n.DEFAULT_LANGUAGE}

    def tags_popular(self):
        return ["музыка", "код"]     # список - как окну, без обёртки

    def nick_probe(self, addr=None):
        return None                  # None - null: сервер не выдумывает успех

    def poll(self, since=0):
        return {"ok": True, "since": since}

    def save_setting(self, request=None):
        return {"ok": True, "key": (request or {}).get("key", "")}

    def start_scan(self):
        # Тот же отказ процесса, что в окне: Api называет причину сам.
        return {"error": "Переиндексация уже идёт"}

    def pick_folder(self):
        return {"ok": True, "path": "должен спросить сервер"}

    def theme_set(self, value=None):
        return {"ok": True}

    def bind_window(self, window):
        return {"ok": True}


class ServeCase(unittest.TestCase):
    """Слушатель на loopback с тестовым профилем (токены, аудит, идентичность)."""

    def setUp(self):
        self._prof = tempfile.TemporaryDirectory()
        self.addCleanup(self._prof.cleanup)
        profile = Path(self._prof.name)
        for module in (identity, channel_mod, serve_tls):
            # Каждый модуль берёт profile_dir своим именем - патчим все: ключи
            # TLS (в том числе временный PEM слушателя) и пины не должны
            # уезжать в настоящий профиль.
            patcher = mock.patch.object(module, "profile_dir", lambda: profile)
            patcher.start()
            self.addCleanup(patcher.stop)
        identity.reset_cache()
        self.addCleanup(identity.reset_cache)
        channel_mod.reset_cache()
        self.addCleanup(channel_mod.reset_cache)
        # Реестр токенов и журнал аудита - в тот же тестовый профиль.
        self.tokens_path = profile / "serve_tokens.bin"
        self.audit_path = profile / "serve_audit.log"
        for module, name, path in ((serve_tokens, "serve_tokens_path",
                                    self.tokens_path),
                                   (serve_audit, "serve_audit_path",
                                    self.audit_path)):
            patcher = mock.patch.object(module, name, lambda path=path: path)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.store = serve_tokens.TokenStore(self.tokens_path)
        self.journal = serve_audit.Audit(self.audit_path)
        self.notes: list[str] = []      # журнал сервера (log=self._log)
        self.read_token = self.store.issue(["read", "watch"], label="читатель")
        self.full_token = self.store.issue(
            ["read", "queue", "mark", "sources", "mesh", "accounts",
             "server_config"], label="полный")

        self.server = serve_http.ServeServer(
            FakeApi(), bind="127.0.0.1", port=0, rate_bps=0, watchers_max=0,
            tokens=self.store, audit=self.journal, language=lambda: "ru",
            log=self.notes.append)
        self.port = self.server.start()
        self.addCleanup(self.server.stop)

        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE

    def tearDown(self):
        self.server.stop()

    # -- помощь ------------------------------------------------------------

    def request(self, method, path, body=None, token=None, raw=None):
        """Запрос к слушателю (TLS, без проверки цепочки: сертификат эфемерный).

        Возвращает (код, тело-JSON или None, текст тела, заголовки) - текст
        нужен для страницы, а не только для /api/*.
        """
        conn = http.client.HTTPSConnection("127.0.0.1", self.port, timeout=10,
                                          context=self.ctx)
        try:
            headers = {}
            if token is not None:
                headers["Authorization"] = "Bearer " + token
            data = raw if raw is not None else (
                "{}" if body is None else json.dumps(body, ensure_ascii=False))
            conn.request(method, path, body=data.encode("utf-8"), headers=headers)
            response = conn.getresponse()
            text = response.read().decode("utf-8")
            try:
                payload = json.loads(text) if text else None
            except json.JSONDecodeError:
                payload = None
            return response.status, payload, text, response.headers
        finally:
            conn.close()

    def post(self, name, body=None, token=None):
        """POST /api/<имя> с ключом «читатель» (read+watch) по умолчанию."""
        return self.request("POST", "/api/" + name, body=body,
                            token=self.read_token.value if token is None else token)

    def get(self, path):
        return self.request("GET", path)

    # -- TLS и страница -----------------------------------------------------

    def test_tls_13_and_san_covers_the_bind_address(self):
        """Браузер верифицирует SAN: адрес привязки обязан быть в сертификате."""
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        tls = self.ctx.wrap_socket(sock, server_hostname="localhost")
        try:
            self.assertEqual(tls.version(), "TLSv1.3",
                             "слушатель не на TLS 1.3 - форвардной секретности нет")
            der = tls.getpeercert(binary_form=True)
        finally:
            tls.close()
        san = x509.load_der_x509_certificate(der).extensions.get_extension_for_class(
            SubjectAlternativeName).value
        dns = [name.value for name in san if isinstance(name, x509.DNSName)]
        ips = [str(ip.value) for ip in san if isinstance(ip, IPAddress)]
        self.assertIn("localhost", dns, "SAN без localhost - браузер откажет")
        self.assertIn("127.0.0.1", ips,
                      "SAN без IP: адрес из цифр как DNS-имя браузер не принимает")

    def test_page_carries_the_server_marker_and_no_token(self):
        """Метка сервера в странице есть, ключа в странице нет (§3, §5)."""
        status, payload, text, headers = self.get("/")
        self.assertEqual(status, 200, payload or text[:80])
        self.assertIn("window.OMNISTASH_HTTP", text)
        self.assertNotIn(self.read_token.value, text,
                         "ключ доступа вшит в страницу - читает любой, кто её получил")
        self.assertEqual(headers.get("Cache-Control"), "no-store",
                         "страница кешится: метка не обновляется после привязки")

    def test_index_html_alias_is_the_same_page(self):
        status, _, text, _ = self.get("/index.html")
        self.assertEqual(status, 200)
        self.assertIn("window.OMNISTASH_HTTP", text)

    def test_get_on_api_is_not_a_method_call(self):
        status, payload, _, _ = self.get("/api/poll")
        self.assertEqual(status, 405)
        self.assertEqual(payload.get("error"), i18n.t("http.post_only", "ru"))
        self.assertIn("метод poll", "\n".join(self.journal.tail()))
        self.assertIn("код 405", "\n".join(self.journal.tail()))

    def test_unknown_path_is_named_not_empty(self):
        status, payload, _, _ = self.get("/nope")
        self.assertEqual(status, 404)
        self.assertEqual(payload.get("error"), i18n.t("http.missing", "ru"))
        self.assertEqual(payload.get("code"), "missing")

    # -- гейт прав ----------------------------------------------------------

    def test_no_token_is_pairing(self):
        status, payload, _, _ = self.request("POST", "/api/poll", token="")
        self.assertEqual(status, 401)
        self.assertEqual(payload.get("code"), "pairing")
        self.assertIn(i18n.t("http.pairing", "ru"), payload.get("error", ""))

    def test_foreign_token_is_pairing_too(self):
        status, payload, _, _ = self.request("POST", "/api/poll",
                                             token="not-from-our-store")
        self.assertEqual(status, 401)
        self.assertEqual(payload.get("code"), "pairing")

    def test_scope_denied_names_the_scope(self):
        status, payload, _, _ = self.post("enqueue")
        self.assertEqual(status, 403)
        self.assertEqual(payload.get("code"), "scope")
        self.assertEqual(payload.get("scope"), "queue")
        self.assertEqual(payload.get("error"),
                         i18n.t("http.scope_denied", "ru") + " queue",
                         "подпись должна называть скоуп, а не «доступ запрещён»")

    def test_native_target_is_refused_with_a_reason(self):
        """Сервер не может открыть диалог на чужой машине - подпись, не молчание."""
        status, payload, _, _ = self.request("POST", "/api/pick_folder",
                                             token=self.full_token.value)
        self.assertEqual(status, 403)
        self.assertEqual(payload.get("code"), "desktop")
        self.assertIn(i18n.t("http.desktop", "ru"), payload.get("error", ""))
        self.assertEqual(payload.get("target"), "pick_folder")

    def test_local_and_internal_targets_are_not_executed(self):
        # Предпочтения клиента живут локально: сервер не применяет их к своему окну.
        status, payload, _, _ = self.request("POST", "/api/theme_set", body="dark",
                                             token=self.full_token.value)
        self.assertEqual(status, 404)
        self.assertEqual(payload.get("code"), "local")
        status, payload, _, _ = self.request("POST", "/api/bind_window",
                                             token=self.full_token.value)
        self.assertEqual(status, 404)
        self.assertEqual(payload.get("code"), "missing")

    def test_target_absent_from_scope_table_is_404(self):
        status, payload, _, _ = self.post("nothing_here")
        self.assertEqual(status, 404)
        self.assertEqual(payload.get("code"), "missing")
        self.assertEqual(payload.get("target"), "nothing_here")

    # -- тело запроса -------------------------------------------------------

    def test_positional_array_matches_the_window_bridge(self):
        """Клиент шлёт [arg] - тот же способ, что pywebview передаёт аргумент."""
        status, payload, _, _ = self.post("poll", body=[5])
        self.assertEqual(status, 200)
        self.assertEqual(payload.get("since"), 5)

    def test_kwargs_object_is_accepted(self):
        status, payload, _, _ = self.post("poll", body={"since": 7})
        self.assertEqual(status, 200)
        self.assertEqual(payload.get("since"), 7)

    def test_empty_body_is_not_a_positional_none(self):
        """{} и пустое тело - ноль аргументов: метод без параметров живёт."""
        status, payload, _, _ = self.post("ui_texts")
        self.assertEqual(status, 200)
        self.assertTrue(payload.get("ok"))
        status, payload, _, _ = self.request("POST", "/api/ui_texts", raw="",
                                             token=self.read_token.value)
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload.get("ok"))

    def test_bad_json_is_named(self):
        status, payload, _, _ = self.request("POST", "/api/poll", raw="не json",
                                             token=self.read_token.value)
        self.assertEqual(status, 400)
        self.assertEqual(payload.get("code"), "bad_json")
        self.assertIn(i18n.t("http.bad_body", "ru"), payload.get("error", ""))

    def test_scalar_body_is_named(self):
        status, payload, _, _ = self.request("POST", "/api/poll", raw="5",
                                             token=self.read_token.value)
        self.assertEqual(status, 400)
        self.assertEqual(payload.get("code"), "bad_json")

    def test_big_body_is_413_not_truncated(self):
        with mock.patch.object(serve_http, "MAX_BODY_BYTES", 128):
            status, payload, _, _ = self.request(
                "POST", "/api/poll", raw=json.dumps({"since": "x" * 2000}),
                token=self.read_token.value)
        self.assertEqual(status, 413)
        self.assertEqual(payload.get("code"), "too_large")
        self.assertIn(i18n.t("http.body_too_large", "ru"), payload.get("error", ""))
        # Тело прочитано и выброшено: соединение не «съедает» следующий запрос.
        status, payload, _, _ = self.post("poll", body=[1])
        self.assertEqual(status, 200)
        self.assertEqual(payload.get("since"), 1)

    def test_wrong_arguments_for_the_signature_are_400(self):
        status, payload, _, _ = self.post("poll", body=[1, 2, 3])
        self.assertEqual(status, 400)
        self.assertEqual(payload.get("code"), "bad_json")

    def test_answer_shapes_mirror_the_window(self):
        """Тело = то, что возвращает pywebview: список списком, None - null.

        Обёртка {"rows": [...]}, {"value": ...} или {"ok": true} вместо None
        - это выдуманный ответ: код клиента читает эти формы иначе, чем в окне.
        """
        status, payload, text, _ = self.request("POST", "/api/tags_popular",
                                                token=self.full_token.value)
        self.assertEqual(status, 200)
        self.assertEqual(payload, ["музыка", "код"],
                         "ответ-список обёртывается: UI читает его как массив")
        self.assertEqual(json.loads(text), ["музыка", "код"])
        status, payload, text, _ = self.request("POST", "/api/nick_probe",
                                                token=self.full_token.value)
        self.assertEqual(status, 200)
        self.assertIsNone(payload, "None стал «успехом» - права и данные выдуманы")
        self.assertEqual(text, "null")
        self.assertIn("пусто", "\n".join(self.journal.tail()),
                      "пустой ответ должен быть виден в журнале как пусто")

    def test_process_refusal_stays_the_api_signature(self):
        """Занятость процесса - 200 с подписью Api: 409 за байтовыми маршрутами."""
        status, payload, _, _ = self.request("POST", "/api/start_scan",
                                             token=self.full_token.value)
        self.assertEqual(status, 200)
        self.assertEqual(payload.get("error"), "Переиндексация уже идёт")
        self.assertIn("отказ", "\n".join(self.journal.tail()))

    # -- журнал аудита ------------------------------------------------------

    def test_audit_records_key_method_code_and_address(self):
        self.post("enqueue")
        lines = self.journal.tail()
        self.assertTrue(lines, "журнал аудита пуст: «кто что спросил» не видно")
        line = lines[-1]
        self.assertIn("ключ читатель", line)
        self.assertIn("метод enqueue", line)
        self.assertIn("код 403", line)
        self.assertIn("право queue", line)
        self.assertIn("клиент 127.0.0.1", line)

    def test_audit_never_contains_tokens_or_paths(self):
        self.post("poll")
        self.request("POST", "/api/pick_folder", token=self.full_token.value)
        self.request("POST", "/api/poll", token="")      # без ключа - кто спросил?
        self.get("/")
        blob = "\n".join(self.journal.tail())
        self.assertIn("не привязан", blob,
                      "обращение без ключа не должно молчать в журнале")
        for value in (self.read_token.value, self.full_token.value):
            self.assertNotIn(value, blob, "значение токена в журнале аудита")
        self.assertFalse(re.search(r"[A-Za-z]:\\", blob),
                         "путь диска в журнале аудита - путь не должен уходить наружу")
        self.assertNotIn("serve_tokens", blob)

    def test_audit_rotates_when_it_grows(self):
        for index in range(200):
            self.journal.record("poll", 200, "127.0.0.1", key_label="читатель")
        with mock.patch.object(serve_audit, "MAX_AUDIT_BYTES", 4000), \
                mock.patch.object(serve_audit, "KEEP_AUDIT_BYTES", 1000):
            self.journal.rotate()
        self.assertLess(self.audit_path.stat().st_size, 4000)
        self.assertIn("rotated", self.audit_path.read_text(encoding="utf-8")[:60])

    def test_tokens_state_has_labels_not_values(self):
        state = self.server.tokens_state()
        self.assertEqual(state["count"], 2)
        self.assertEqual(sorted(item["label"] for item in state["tokens"]),
                         ["полный", "читатель"])
        for item in state["tokens"]:
            self.assertNotIn("value", item)
            self.assertIn("scopes", item)

    def test_state_before_read_is_honest(self):
        """Реестр читается при старте: «ключей ноль» до чтения - не правда."""
        fresh = self.tokens_path.parent / "fresh.bin"
        with mock.patch.object(serve_tokens, "serve_tokens_path", lambda: fresh):
            server = serve_http.ServeServer(FakeApi(), bind="127.0.0.1", port=0,
                                            audit=self.journal)
            state = server.tokens_state()
        self.assertEqual(state["count"], 0)
        self.assertTrue(state.get("error"),
                        "не прочитанный реестр должен быть назван причиной")

    # -- состояние слушателя ------------------------------------------------

    def test_journal_fingerprint_is_the_served_key(self):
        """Отпечаток, названный сервером при подъёме, = ключ этого соединения.

        Без этого журнал мог бы показать чужой ключ: человек сверяет именно
        эту строку с тем, что запомнил клиент (TOFU, §5).
        """
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        tls = self.ctx.wrap_socket(sock, server_hostname="localhost")
        try:
            der = tls.getpeercert(binary_form=True)
        finally:
            tls.close()
        served = x509.load_der_x509_certificate(der)
        spki = served.public_key().public_bytes(
            Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        shown = [line for line in self.notes if "отпечаток ключа страницы" in line]
        self.assertTrue(shown, "журнал не называет отпечаток ключа страницы")
        match = re.search(r"отпечаток ключа страницы ([0-9a-f]+)", shown[0])
        self.assertTrue(match, f"отпечаток не читаемый: {shown[0]!r}")
        digest = hashlib.sha256(spki).hexdigest()
        self.assertEqual(digest[:len(match.group(1))], match.group(1),
                         "отпечаток в журнале ≠ SHA-256 от SPKI того сертификата, "
                         "что обслуживает соединение")

    def test_base_url_is_https_on_the_bind_address(self):
        self.assertTrue(self.server.base_url().startswith("https://127.0.0.1:"))
        self.assertEqual(self.server.port, self.port)

    def test_token_issued_after_start_is_accepted(self):
        """Ключ, выданный уже поднятому слушателю, принимается.

        pairing (S6) пишет реестр тем же файлом, пока слушатель живёт: реестр,
        прочитанный один раз при старте, отказал бы новому ключу молча.
        """
        other = serve_tokens.TokenStore(self.tokens_path)   # другой писатель
        fresh = other.issue(["read"], label="новый ключ")
        status, payload, _, _ = self.post("poll", token=fresh.value)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload.get("since"), 0)
        # Отзов того же ключа снимает право - тоже без перезапуска.
        other.revoke(fresh.id)
        status, payload, _, _ = self.post("poll", token=fresh.value)
        self.assertEqual(status, 401, payload)
        self.assertEqual(payload.get("code"), "pairing")

    def test_rate_limit_and_viewers_reach_the_handler(self):
        server = serve_http.ServeServer(FakeApi(), bind="127.0.0.1", port=0,
                                        rate_bps=1_000_000, watchers_max=3,
                                        tokens=self.store, audit=self.journal)
        server.start()
        try:
            handler = server._httpd.RequestHandlerClass
            self.assertEqual(handler.rate_bps, 1_000_000)
            self.assertEqual(handler.watchers_max, 3)
        finally:
            server.stop()

    def test_stop_then_start_again_works(self):
        self.server.stop()
        self.assertFalse(self.server.running)
        self.server.stop()              # повторный stop не валит выход
        port = self.server.start()
        self.port = port                # системный порт новый: запросы на него
        status, _, text, _ = self.get("/")
        self.assertEqual(status, 200, text[:80])
        self.assertEqual(port, self.server.port)


class TestServeTlsMaterial(unittest.TestCase):
    """Материал TLS для браузера: ECDSA, SAN, отпечаток того же ключа.

    Причина отдельного модуля от mesh.channel - замер на этой машине:
    in-app Chromium на loopback отвергает Ed25519 рукопожатием
    (ERR_SSL_VERSION_OR_CIPHER_MISMATCH), а ECDSA и RSA доходят до решения
    человека о доверии (ERR_CERT_AUTHORITY_INVALID).
    """

    def setUp(self):
        self._prof = tempfile.TemporaryDirectory()
        self.addCleanup(self._prof.cleanup)
        profile = Path(self._prof.name)
        for module in (serve_tls, channel_mod):
            patcher = mock.patch.object(module, "profile_dir", lambda: profile)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.profile = profile

    def test_material_is_ecdsa_not_ed25519(self):
        cert = x509.load_pem_x509_certificate(serve_tls.material()[0])
        self.assertIsInstance(cert.public_key(), ec.EllipticCurvePublicKey,
                              "браузерный сертификат не на ECDSA: TLS-стек "
                              "Chromium отвергает Ed25519 до предупреждения")
        self.assertNotIsInstance(cert.public_key(), Ed25519PublicKey,
                                  "слушатель клиента получил mesh-ключ")

    def test_san_covers_ip_and_dns(self):
        cert = x509.load_pem_x509_certificate(
            serve_tls.material(["localhost", "127.0.0.1"])[0])
        san = cert.extensions.get_extension_for_class(SubjectAlternativeName).value
        self.assertTrue(any(isinstance(name, x509.DNSName)
                            and name.value == "localhost" for name in san))
        self.assertTrue(any(isinstance(ip, IPAddress)
                            and str(ip.value) == "127.0.0.1" for ip in san),
                        "IP в SAN отдельным типом: DNS-имя из цифр браузер не принимает")

    def test_empty_hosts_gives_no_san(self):
        cert = x509.load_pem_x509_certificate(serve_tls.material([])[0])
        self.assertFalse([ext for ext in cert.extensions
                          if ext.oid == SubjectAlternativeName.oid])

    def test_key_does_not_stay_on_disk(self):
        serve_tls.context_from(*serve_tls.material(["127.0.0.1"]))
        left = [path.name for path in self.profile.iterdir() if path.suffix == ".pem"]
        self.assertEqual(left, [],
                         "эфемерный ключ TLS осел на диске: его должен читать только OpenSSL в памяти")

    def test_fingerprint_is_the_spki_of_the_certificate(self):
        """Отпечаток считается по SPKI, а не по всему сертификату (§5)."""
        cert_pem, _key_pem = serve_tls.material(["localhost", "127.0.0.1"])
        cert = x509.load_pem_x509_certificate(cert_pem)
        spki = cert.public_key().public_bytes(
            Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        self.assertEqual(serve_tls.fingerprint(cert_pem),
                         hashlib.sha256(spki).hexdigest())
        self.assertNotEqual(serve_tls.fingerprint(cert_pem),
                            hashlib.sha256(cert.public_bytes(Encoding.DER)).hexdigest(),
                            "отпечаток по всему сертификату - клиент бы не совпал")

    def test_mesh_channel_material_unchanged(self):
        """Mesh-канал не получил SAN и не сменил ключ: привязка там аттестацией."""
        cert = x509.load_pem_x509_certificate(channel_mod.server_material()[0])
        self.assertFalse([ext for ext in cert.extensions
                          if ext.oid == SubjectAlternativeName.oid],
                         "mesh-сертификат получил SAN - лишняя проверка hostname")
        self.assertIsInstance(cert.public_key(), Ed25519PublicKey,
                              "mesh-канал сменил Ed25519: аттестация SPKI сломалась бы")

    def test_bind_address_never_opens_outward_in_s1(self):
        """Случайный текст в настройке не должен привязать порт наружу."""
        for value in ("loopback", "localhost", "127.0.0.1", "", None,
                      "0.0.0.0", "lan", "192.168.1.5"):
            self.assertEqual(serve_http.bind_address(value), "127.0.0.1",
                             f"{value}: S1 не имеет права слушать не loopback")


class TestTokenStore(unittest.TestCase):
    """Реестр ключей доступа: DPAPI, лимит, отзов, битый файл назван причиной."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "serve_tokens.bin"

    def store(self, max_tokens: int = serve_tokens.MAX_TOKENS):
        return serve_tokens.TokenStore(self.path, max_tokens=max_tokens)

    def test_roundtrip_through_dpapi(self):
        store = self.store()
        token = store.issue(["read"], label="читатель")
        self.assertNotIn(token.value.encode(), self.path.read_bytes(),
                         "токен лежит на диске открытым текстом")
        reopened = self.store()
        found = reopened.verify(token.value)
        self.assertIsNotNone(found, "DPAPI-раундтрип не читает токен")
        self.assertEqual(found.scopes, ("read",))
        self.assertIsNone(reopened.verify(token.value + "-чужое"))

    def test_empty_file_is_not_an_error(self):
        self.assertEqual(self.store().count(), 0)

    def test_broken_file_is_named_as_a_reason(self):
        self.path.write_bytes("не DPAPI вовсе".encode("utf-8"))
        with self.assertRaises(serve_tokens.TokenStoreError) as caught:
            self.store()
        self.assertIn(i18n.t("http.tokens_broken", "ru"), str(caught.exception))

    def test_limit_is_an_honest_refusal(self):
        store = self.store(max_tokens=2)
        store.issue(["read"], label="а")
        store.issue(["read"], label="б")
        with self.assertRaises(serve_tokens.TokenStoreError) as caught:
            store.issue(["read"], label="в")
        self.assertIn(i18n.t("http.tokens_limit", "ru"), str(caught.exception))
        self.assertIn("2", str(caught.exception))
        self.assertEqual(store.count(), 2, "лимит не должен менять реестр")

    def test_empty_scope_list_refused(self):
        with self.assertRaises(ValueError) as caught:
            self.store().issue([], label="пусто")
        self.assertIn(i18n.t("http.scopes_none", "ru"), str(caught.exception))

    def test_unknown_scope_rejected_not_dropped(self):
        """Мусорный скоуп - отказ, а не тихое «право пропало, доступ полный»."""
        with self.assertRaises(ValueError) as caught:
            serve_tokens.normalize_scopes(["read", "всё"])
        self.assertIn("неизвестный скоуп", str(caught.exception))
        self.path.write_bytes(serve_tokens.encrypt(json.dumps(
            {"tokens": [{"id": "x", "label": "битый", "scopes": ["всё"],
                        "created_at": "2026-11-03 00:00:00",
                        "value": "секрет"}]}, ensure_ascii=False).encode("utf-8")))
        with self.assertRaises(serve_tokens.TokenStoreError) as caught:
            self.store()
        self.assertIn("неизвестный скоуп", str(caught.exception))

    def test_revoke_reports_absent_id(self):
        store = self.store()
        token = store.issue(["read"], label="читатель")
        self.assertFalse(store.revoke("нет-такого"),
                         "отзов несуществующего ключа не должен выглядеть успехом")
        self.assertTrue(store.revoke(token.id))
        self.assertEqual(store.count(), 0)
        self.assertIsNone(self.store().verify(token.value))

    def test_public_record_has_no_value(self):
        record = self.store().issue(["read", "watch"], label="читатель").public()
        self.assertNotIn("value", record)
        self.assertEqual(record["scopes"], ["read", "watch"])

    def test_scopes_are_deduplicated(self):
        self.assertEqual(serve_tokens.normalize_scopes(["read", "read", "watch"]),
                         ("read", "watch"))

    def test_another_writer_is_seen_without_restart(self):
        """Живой реестр перечитывает файл: отзов и выдача видны без перезапуска."""
        reader = self.store()
        writer = self.store()
        self.assertEqual(reader.count(), 0)
        token = writer.issue(["read"], label="читатель")
        self.assertIsNotNone(reader.verify(token.value),
                             "живой слушатель не увидел бы выданный ключ")
        self.assertEqual(reader.count(), 1)
        self.assertTrue(writer.revoke(token.id))
        self.assertIsNone(reader.verify(token.value),
                          "отзов не снял право у живого слушателя")
        self.assertEqual(reader.count(), 0)

    def test_max_tokens_default_is_small(self):
        """Перебор ключей ограничен: 8 живущих ключей - не «сколько угодно»."""
        self.assertEqual(serve_tokens.MAX_TOKENS, 8)


class TestScopeParity(unittest.TestCase):
    """Скоупы - единственный источник правды: фасад, клиент, документ."""

    def test_scope_module_covers_the_backend_exactly(self):
        backend = api_public_names()
        covered = set(serve_scopes.SCOPE_OF) | serve_scopes.NATIVE \
            | serve_scopes.LOCAL | serve_scopes.INTERNAL
        self.assertEqual(sorted(covered), sorted(backend),
                         "списки скоупов разошлись с публичными методами Api")
        self.assertEqual(len(covered), len(backend),
                         "одна цель стоит в двух списках скоупов")

    def test_js_lists_match_the_scope_module(self):
        self.assertEqual(set(js_list("HTTP_METHODS")), set(serve_scopes.SCOPE_OF))
        self.assertEqual(set(js_list("HTTP_DESKTOP")), serve_scopes.NATIVE)
        self.assertEqual(set(js_list("HTTP_LOCAL")), serve_scopes.LOCAL)
        self.assertEqual(set(js_list("HTTP_INTERNAL")), serve_scopes.INTERNAL)

    def test_watch_scope_is_bytes_only(self):
        self.assertIn("watch", serve_scopes.SCOPES)
        self.assertEqual([name for name, scope in serve_scopes.SCOPE_OF.items()
                          if scope == "watch"], [],
                         "watch не должен требовать методов Api")

    def test_every_scope_except_bytes_and_native_has_targets(self):
        """`watch` - байтовые маршруты, `desktop` - группа нативных целей:
        оба не требуют методов Api, поэтому в SCOPE_OF их нет."""
        used = set(serve_scopes.SCOPE_OF.values())
        self.assertEqual(used, set(serve_scopes.SCOPES) - {"watch", "desktop"},
                         "скоуп в списке прав, но ни одна цель его не требует")
        self.assertTrue(all(scope in serve_scopes.SCOPES
                            for scope in serve_scopes.SCOPE_OF.values()),
                        "цель требует скоуп, которого в списке прав нет")

    def test_doc_table_matches_the_scope_module(self):
        doc = DOC.read_text(encoding="utf-8")
        section = re.search(r"## 4\..*?(?=## 5\.)", doc, re.DOTALL)
        assert section, "в docs/SERVER-CLIENT.md нет раздела §4 со скоупами"
        by_scope: dict[str, set] = {}
        for line in re.findall(r"^\| `[a-z_]+` \|.*$", section.group(0),
                               re.MULTILINE):
            cells = line.split("|")
            scope = cells[1].strip().strip("`")
            by_scope[scope] = set(re.findall(r"`([a-z_]+)`", cells[-2]))
        self.assertEqual(set(by_scope), set(serve_scopes.SCOPES),
                         "названия скоупов в документе разошлись со списком прав")
        for scope, names in by_scope.items():
            if scope == "watch":
                self.assertEqual(names, set(), "документ обещает методы для watch")
                continue
            if scope == "desktop":
                self.assertEqual(names, serve_scopes.NATIVE,
                                 "документ разошёлся со списком нативных целей")
                continue
            self.assertEqual(names,
                             {name for name, value in serve_scopes.SCOPE_OF.items()
                              if value == scope},
                             f"таблица §4 ({scope}) разошлась с app/http/scopes.py")
        extra: set = set()
        for line in re.findall(r"^- `.*$", section.group(0), re.MULTILINE):
            extra |= set(re.findall(r"`([a-z_]+)`", line))
        self.assertEqual(extra, serve_scopes.LOCAL | serve_scopes.INTERNAL,
                         "документ разошёлся со списком локальных целей")

    def test_adult_gate_is_server_side(self):
        """Клиент не обходит 18+: подтверждение - queue, флаг вкладки - server_config."""
        self.assertEqual(serve_scopes.scope_for("legal_accept"), "queue")
        self.assertEqual(serve_scopes.scope_for("adult_tab_show"), "server_config")
        self.assertEqual(serve_scopes.scope_for("adult_tab"), "read")

    def test_thumbs_are_read_scope_not_watch(self):
        """Обложки - чтение (read), байты медиа - watch (§3: разные маршруты)."""
        self.assertEqual(serve_scopes.scope_for("get_thumb"), "read")
        self.assertEqual(serve_scopes.scope_for("get_thumbs"), "read")


class TestGuiWiring(GuiCase):
    """Фасад окна: слушатель следует настройке и закрытию процесса."""

    class Recorder:
        """Заменяет ServeServer: видно, что окно ему приказало, без сокета."""

        def __init__(self, api, **kwargs):
            self.kwargs = kwargs
            self.running = False
            self.started = 0
            self.stopped = 0
            self.fail_start = False

        def start(self):
            if self.fail_start:
                raise serve_http.ServeError("порт занят другим процессом")
            self.started += 1
            self.running = True
            return int(self.kwargs.get("port") or 0)

        def stop(self):
            self.running = False
            self.stopped += 1

        def base_url(self):
            return f"https://{self.kwargs.get('bind')}:{self.kwargs.get('port')}"

        def tokens_state(self):
            return {"tokens": [], "count": 0}

    def setUp(self):
        super().setUp()
        self.built: list[TestGuiWiring.Recorder] = []

        def build(api, **kwargs):
            instance = self.Recorder(api, **kwargs)
            self.built.append(instance)
            return instance

        patcher = mock.patch.object(serve_http, "ServeServer", build)
        patcher.start()
        self.addCleanup(patcher.stop)

    def lines(self, api) -> list[str]:
        """Смотрим журнал, а не файл: строки `_log` этого Api."""
        got: list[str] = []
        api._log = got.append
        return got

    def test_flag_off_leaves_no_listener(self):
        api = self.make_api()
        state = api._serve_state()
        self.assertFalse(state["enabled"],
                         "serve_enabled по умолчанию должен быть выключен")
        self.assertFalse(state["running"],
                         "выключенный доступ поднял слушателя")
        self.assertEqual(self.built[0].started, 0,
                         "без согласия слушатель стартовал")
        self.assertEqual(self.built[0].kwargs["bind"], "127.0.0.1")
        self.assertEqual(self.built[0].kwargs["port"], 1001,
                         "порт по умолчанию - 1001 (рядом с mesh 1000)")

    def test_flag_on_starts_once_and_says_so(self):
        api = self.make_api()
        notes = self.lines(api)
        api.save_setting({"key": "serve_enabled", "value": True})
        self.assertEqual(self.built[-1].started, 1)
        self.assertTrue(api._serve_state()["running"])
        self.assertTrue(any("Доступ по HTTPS поднят" in line for line in notes),
                        "журнал не говорит, что слушатель поднят")
        api.save_setting({"key": "serve_enabled", "value": True})
        self.assertEqual(self.built[-1].started, 1,
                         "повторное включение поднимает слушателя второй раз")

    def test_flag_off_stops_the_listener(self):
        api = self.make_api()
        api.save_setting({"key": "serve_enabled", "value": True})
        recorder = self.built[-1]
        api.save_setting({"key": "serve_enabled", "value": False})
        self.assertEqual(recorder.stopped, 1,
                         "выключенный доступ оставил порт живым")
        self.assertFalse(api._serve_state()["running"])

    def test_port_change_rebuilds_the_listener(self):
        api = self.make_api()
        api.save_setting({"key": "serve_enabled", "value": True})
        first = self.built[-1]
        api.save_setting({"key": "serve_port", "value": 444})
        second = self.built[-1]
        self.assertIsNot(second, first, "смена порта не пересоздала слушателя")
        self.assertEqual(first.stopped, 1, "старый слушатель не снят")
        self.assertEqual(second.kwargs["port"], 444)
        self.assertEqual(second.started, 1, "новый слушатель не поднят")

    def test_rate_and_viewers_follow_the_settings(self):
        api = self.make_api()
        api.save_setting({"key": "serve_enabled", "value": True})
        first = self.built[-1]
        self.assertEqual(first.kwargs["rate_bps"], 10 * 1_000_000 // 8,
                         "serve_rate_mbit не переведён в байты в секунду")
        self.assertEqual(first.kwargs["watchers_max"], 4)
        api.save_setting({"key": "serve_rate_mbit", "value": 0})
        self.assertEqual(self.built[-1].kwargs["rate_bps"], 0,
                         "0 должен означать «без лимита»")
        self.assertEqual(first.stopped, 1, "смена лимита не сняла старый обработчик")

    def test_start_failure_is_logged_not_swallowed(self):
        api = self.make_api()
        notes = self.lines(api)
        api.save_setting({"key": "serve_enabled", "value": True})
        recorder = self.built[-1]
        recorder.fail_start = True
        recorder.running = False       # слушатель «упал» при подъёме
        api._serve_apply()
        self.assertTrue(any("Доступ по HTTPS не поднят" in line for line in notes),
                        "отказ старта обязан быть виден в журнале")
        self.assertFalse(api._serve_state()["running"])

    def test_close_stops_the_listener(self):
        api = Api()
        api.save_setting({"key": "serve_enabled", "value": True})
        recorder = self.built[-1]
        self.assertTrue(recorder.running)
        api.close()
        self.assertFalse(recorder.running,
                         "слушатель пережил закрытие процесса")
        self.assertEqual(recorder.stopped, 1)

    def test_serve_state_reports_keys_without_values(self):
        api = self.make_api()
        api.save_setting({"key": "serve_enabled", "value": True})
        state = api._serve_state()
        self.assertEqual(state["count"], 0,
                         "ключей реально нет - состояние не выдумывает их")
        self.assertEqual(state["tokens"], [])
        self.assertEqual(state["url"], self.built[-1].base_url())
        self.assertEqual(state["port"], self.built[-1].kwargs["port"])
        self.assertEqual(state["bind"], "127.0.0.1")


class TestServeSettingsCard(unittest.TestCase):
    """Поля доступа по HTTPS: схема, подпись раздела и зеркало превью."""

    def test_fields_exist_with_honest_defaults(self):
        defaults = settings_schema.defaults()
        self.assertIs(defaults["serve_enabled"], False,
                      "доступ наружу по умолчанию выключен")
        self.assertEqual(defaults["serve_bind"], "loopback")
        self.assertEqual(defaults["serve_port"], 1001)
        self.assertEqual(defaults["serve_rate_mbit"], 10)
        self.assertEqual(defaults["serve_watchers_max"], 4)
        self.assertIs(defaults["serve_hls_transcode"], False,
                      "ре-encode по умолчанию выключен (§6)")

    def test_port_is_outside_the_dpi_zone_by_default(self):
        self.assertFalse(mesh_firewall.in_dpi_zone(1001),
                         "порт по умолчанию в зоне перехвата DPI-утилит")
        field = settings_schema.field("serve_port")
        self.assertIn("DPI", field["hint"], "пояснение порта не говорит про перехват")

    def test_section_is_translated(self):
        names = {field["section"] for field in settings_schema.schema()}
        self.assertIn("Доступ по HTTPS", names)
        self.assertEqual(i18n.sections("en")["Доступ по HTTPS"], "HTTPS access")
        self.assertEqual(i18n.sections("ru")["Доступ по HTTPS"], "Доступ по HTTPS")

    def test_second_language_covers_every_new_field(self):
        labels = i18n.schema_labels("en")
        for key in ("serve_enabled", "serve_bind", "serve_port",
                    "serve_rate_mbit", "serve_watchers_max",
                    "serve_hls_transcode"):
            self.assertIn(key, labels, f"карточка на английском не переводит {key}")
            self.assertTrue(labels[key].get("label"), f"{key}: нет подписи")
            self.assertTrue(labels[key].get("hint"), f"{key}: нет пояснения")
            for value in labels[key].values():
                self.assertFalse(any(char in "абвгдежзийклмнопрстуфхцчшщэюя"
                                     for char in value), f"{key}: en с кириллицей")

    def test_bind_choice_has_only_loopback_in_s1(self):
        choices = settings_schema.field("serve_bind")["choices"]
        self.assertEqual([pair[0] for pair in choices], ["loopback"],
                         "S1 не имеет права предлагать выход наружу (это S6)")

    def test_server_signatures_exist_in_both_languages(self):
        for key in ("http.scope_denied", "http.post_only", "http.bad_body",
                    "http.body_too_large", "http.scopes_none",
                    "http.tokens_broken", "http.tokens_dpapi", "http.tokens_limit"):
            self.assertTrue(i18n.has(key), f"{key} нет в словаре")
            self.assertTrue(i18n.t(key, "ru"), f"{key} без русской подписи")
            en = i18n.t(key, "en")
            self.assertTrue(en, f"{key} без английского варианта")
            self.assertFalse(any(char in "абвгдежзийклмнопрстуфхцчшщэюя"
                                 for char in en), f"{key}: en с кириллицей")


if __name__ == "__main__":
    unittest.main()
