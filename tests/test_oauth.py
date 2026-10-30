"""OAuth (G2: установленный клиент + PKCE + loopback + хранение refresh-токена в DPAPI).

Ни один тест не ходит в сеть: loopback-сервер - реальный сокет на 127.0.0.1
(это не внешняя сеть), обмен и refresh - мок _post. Профиль - изолированный
tmp через env LOCALAPPDATA (как в test_google_account)."""

import base64
import hashlib
import io
import os
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

from app import oauth
from app import paths


def http_error(code: int, body: bytes) -> urllib.error.HTTPError:
    """HTTP-ошибка OAuth endpoint с JSON-телом (для мока urlopen)."""
    return urllib.error.HTTPError("https://oauth2.googleapis.com/token", code,
                                  "Forbidden", {}, io.BytesIO(body))


class ProfileCase(unittest.TestCase):
    """Изолированный профиль: токены не трогают настоящий профиль человека."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ,
                                  {"LOCALAPPDATA": tempfile.mkdtemp(prefix="oa_")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.profile = Path(os.environ["LOCALAPPDATA"]) / "Omnistash"

    def tokens_file(self) -> Path:
        return paths.google_tokens_path()


class TestPkce(unittest.TestCase):
    def test_verifier_charset_and_boundaries(self):
        verifier, _ = oauth.pkce_pair()
        self.assertGreaterEqual(len(verifier), 43)
        self.assertLessEqual(len(verifier), 128)
        self.assertRegex(verifier, r"^[A-Za-z0-9\-_]+$")

    def test_challenge_is_sha256_base64url_without_padding(self):
        verifier, challenge = oauth.pkce_pair()
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=")
        self.assertEqual(challenge, expected.decode("ascii"))
        self.assertNotIn("=", challenge)

    def test_each_call_returns_new_verifier(self):
        self.assertEqual(len({oauth.pkce_pair()[0], oauth.pkce_pair()[0]}), 2)


class TestAuthorizeUrl(unittest.TestCase):
    def test_params_for_installed_app(self):
        url = oauth.authorize_url("cid-123", "http://127.0.0.1:5000/",
                                  "state-x", "challenge-y")
        parsed = urllib.parse.urlparse(url)
        self.assertEqual(parsed.netloc, "accounts.google.com")
        self.assertEqual(parsed.path, "/o/oauth2/v2/auth")
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(query["client_id"], ["cid-123"])
        self.assertEqual(query["redirect_uri"], ["http://127.0.0.1:5000/"])
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["access_type"], ["offline"])
        self.assertEqual(query["code_challenge"], ["challenge-y"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["state"], ["state-x"])
        self.assertIn("youtube.readonly", query["scope"][0])

    def test_no_client_secret_anywhere_in_url(self):
        url = oauth.authorize_url("cid", "http://127.0.0.1:1/", "s", "ch")
        self.assertNotIn("client_secret", url.lower())


class TestLoopback(unittest.TestCase):
    def _server(self, state="expected-state"):
        server = oauth._start_loopback(state)
        self.addCleanup(lambda: oauth._shutdown(server))
        return server

    def test_bind_is_local_only_and_ephemeral(self):
        server = self._server()
        self.assertEqual(server.server_address[0], "127.0.0.1")
        self.assertGreater(server.server_port, 0)

    def test_loopback_captures_code_and_state(self):
        server = self._server()
        urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_port}/?code=abc&state=expected-state",
            timeout=5).read()
        self.assertTrue(server.done.wait(5))
        self.assertEqual(server.result.get("code"), "abc")

    def test_loopback_state_mismatch_is_recorded(self):
        server = self._server("our-state")
        urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_port}/?code=abc&state=other",
            timeout=5).read()
        self.assertTrue(server.done.wait(5))
        self.assertTrue(server.result.get("state_mismatch"))
        self.assertNotIn("code", server.result)

    def test_loopback_ignores_requests_without_code(self):
        server = self._server()
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/favicon.ico",
                                   timeout=5)
            self.fail("запрос без кода должен быть 404, а не страница")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 404)
            exc.close()
        self.assertFalse(server.done.wait(0.1))
        urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_port}/?error=access_denied"
            "&state=expected-state", timeout=5).read()
        self.assertTrue(server.done.wait(5))
        self.assertEqual(server.result.get("error"), "access_denied")


class TestRunFlow(ProfileCase):
    def _opener(self, captured):
        """Браузер из теста: сам делает возврат на loopback с кодом и state."""
        def opener(url: str) -> None:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            redirect = query["redirect_uri"][0]
            state = query["state"][0]
            captured["url"] = url
            urllib.request.urlopen(f"{redirect}?code=code-1&state={state}",
                                   timeout=5).read()
        return opener

    def test_flow_happy_stores_refresh_encrypted(self):
        state = {}
        captured = {}
        with mock.patch.object(oauth, "exchange_code",
                               return_value={"refresh_token": "rt-secret-9",
                                             "access_token": "at",
                                             "scope": "s"}):
            result = oauth.run_flow("cid", state=state,
                                    opener=self._opener(captured),
                                    timeout=10, log=lambda text: None)
        self.assertTrue(result["ok"], result)
        self.assertEqual(state["phase"], "done")
        self.assertIn("cid", captured["url"])        # opener видел authorize URL
        self.assertIn("code_challenge", captured["url"])
        raw = self.tokens_file().read_bytes()
        self.assertNotIn(b"rt-secret-9", raw, "на диске должен лежать шифротекст")
        tokens = oauth.load_tokens()
        self.assertEqual(tokens["refresh_token"], "rt-secret-9")
        self.assertEqual(tokens["client_id"], "cid")
        self.assertFalse(tokens["broken"])
        status = oauth.status({"oauth_client_id": "cid"})
        self.assertTrue(status["signed_in"])
        self.assertTrue(status["configured"])

    def test_flow_timeout_honest_without_token(self):
        state = {}
        result = oauth.run_flow("cid", state=state,
                                opener=lambda url: None, timeout=0.3, log=None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["kind"], "cancelled")
        self.assertIn("не вернулся", result["error"])
        self.assertFalse(self.tokens_file().exists())

    def test_flow_denied_from_consent(self):
        def opener(url: str) -> None:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            redirect = query["redirect_uri"][0]
            urllib.request.urlopen(
                f"{redirect}?error=access_denied&state={query['state'][0]}",
                timeout=5).read()

        result = oauth.run_flow("cid", opener=opener, timeout=10)
        self.assertFalse(result["ok"])
        self.assertEqual(result["kind"], "cancelled")
        self.assertIn("отказал", result["error"])
        self.assertFalse(self.tokens_file().exists())

    def test_flow_state_mismatch_rejects_flow(self):
        def opener(url: str) -> None:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            redirect = query["redirect_uri"][0]
            urllib.request.urlopen(f"{redirect}?code=code-1&state=wrong-state",
                                   timeout=5).read()

        result = oauth.run_flow("cid", opener=opener, timeout=10)
        self.assertFalse(result["ok"])
        self.assertIn("не тот state", result["error"])
        self.assertFalse(self.tokens_file().exists())

    def test_flow_exchange_failure_leaves_no_token(self):
        with mock.patch.object(oauth, "exchange_code",
                               side_effect=oauth.FlowError(
                                   "код не принят: повторите вход",
                                   kind="invalid_grant")):
            result = oauth.run_flow("cid", opener=self._opener({}),
                                    timeout=10)
        self.assertFalse(result["ok"])
        self.assertEqual(result["kind"], "invalid_grant")
        self.assertFalse(self.tokens_file().exists())

    def test_flow_without_client_id_honest(self):
        state = {}
        result = oauth.run_flow("", state=state)
        self.assertFalse(result["ok"])
        self.assertEqual(result["kind"], "unsupported")
        self.assertEqual(state["phase"], "error")
        self.assertIn("client_id", result["error"])


class TestTokenEndpoints(ProfileCase):
    def test_exchange_body_has_verifier_and_no_client_secret(self):
        captured = {}

        def fake_post(url, body, timeout=oauth.TOKEN_TIMEOUT):
            captured["url"] = url
            captured["body"] = body
            return {"access_token": "at", "refresh_token": "rt", "scope": "s"}

        with mock.patch.object(oauth, "_post", fake_post):
            tokens = oauth.exchange_code("cid", "code-1",
                                         "http://127.0.0.1:9/", "ver-1")
        self.assertEqual(captured["url"], oauth.TOKEN_ENDPOINT)
        self.assertEqual(captured["body"]["grant_type"], "authorization_code")
        self.assertEqual(captured["body"]["code_verifier"], "ver-1")
        self.assertEqual(captured["body"]["client_id"], "cid")
        self.assertNotIn("client_secret", captured["body"])
        self.assertEqual(tokens["refresh_token"], "rt")

    def test_exchange_http_invalid_grant_is_honest(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=http_error(400, b'{"error": "invalid_grant"}')):
            with self.assertRaises(oauth.FlowError) as caught:
                oauth.exchange_code("cid", "code-1", "http://127.0.0.1:9/", "ver")
        self.assertEqual(caught.exception.kind, "invalid_grant")
        self.assertIn("повторите вход", str(caught.exception))

    def test_exchange_without_refresh_token_is_honest(self):
        with mock.patch.object(oauth, "_post", return_value={"access_token": "at"}):
            with self.assertRaises(oauth.FlowError) as caught:
                oauth.exchange_code("cid", "code-1", "http://127.0.0.1:9/", "ver")
        self.assertEqual(caught.exception.kind, "empty")

    def test_refresh_request_uses_client_id_without_secret(self):
        captured = {}

        def fake_post(url, body, timeout=oauth.TOKEN_TIMEOUT):
            captured["body"] = body
            return {"access_token": "at"}

        with mock.patch.object(oauth, "_post", fake_post):
            access = oauth.refresh_access_token("cid", "rt-1")
        self.assertEqual(captured["body"]["grant_type"], "refresh_token")
        self.assertEqual(captured["body"]["refresh_token"], "rt-1")
        self.assertEqual(captured["body"]["client_id"], "cid")
        self.assertNotIn("client_secret", captured["body"])
        self.assertEqual(access, "at")

    def test_invalid_client_hint_mentions_setting(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=http_error(401, b'{"error": "invalid_client"}')):
            with self.assertRaises(oauth.FlowError) as caught:
                oauth.refresh_access_token("bad", "rt")
        self.assertEqual(caught.exception.kind, "blocked")
        self.assertIn("OAuth client_id", str(caught.exception))

    def test_access_token_returns_fresh_access(self):
        oauth.save_tokens("rt-1", scope="s", client_id="cid")
        with mock.patch.object(oauth, "refresh_access_token",
                               return_value="at-2"):
            self.assertEqual(oauth.access_token({"oauth_client_id": "cid"}), "at-2")

    def test_access_token_invalid_grant_forgets_token(self):
        oauth.save_tokens("rt-old", scope="s", client_id="cid")
        with mock.patch.object(oauth, "refresh_access_token",
                               side_effect=oauth.FlowError("bad",
                                                           kind="invalid_grant")):
            with self.assertRaises(oauth.FlowError) as caught:
                oauth.access_token({"oauth_client_id": "cid"})
        self.assertEqual(caught.exception.kind, "invalid_grant")
        self.assertIn("войдите заново", str(caught.exception))
        self.assertFalse(self.tokens_file().exists())

    def test_access_token_none_when_not_signed_in(self):
        self.assertIsNone(oauth.access_token({"oauth_client_id": "cid"}))


class TestStorageAndForget(ProfileCase):
    def test_save_forget_roundtrip(self):
        saved = oauth.save_tokens("rt", scope="s", client_id="cid")
        self.assertTrue(self.tokens_file().exists())
        self.assertTrue(saved["since"])
        forget = oauth.forget()
        self.assertTrue(forget["removed"])
        self.assertFalse(self.tokens_file().exists())
        self.assertFalse(oauth.forget()["removed"])
        self.assertIsNone(oauth.load_tokens())

    def test_status_without_client_id_honest(self):
        status = oauth.status({})
        self.assertFalse(status["configured"])
        self.assertFalse(status["signed_in"])
        self.assertIn("не задано", status["hint"].lower())

    def test_corrupt_file_marked_broken_and_not_used(self):
        self.tokens_file().parent.mkdir(parents=True, exist_ok=True)
        self.tokens_file().write_bytes("мусор".encode("utf-8"))
        tokens = oauth.load_tokens()
        self.assertTrue(tokens["broken"])
        status = oauth.status({"oauth_client_id": "cid"})
        self.assertFalse(status["signed_in"])
        self.assertIn("не читаются", status["hint"])
        with self.assertRaises(oauth.FlowError) as caught:
            oauth.access_token({"oauth_client_id": "cid"})
        self.assertEqual(caught.exception.kind, "unsupported")

    def test_status_hides_token_values(self):
        oauth.save_tokens("rt-секрет", scope="s", client_id="cid")
        status = oauth.status({"oauth_client_id": "cid"})
        self.assertTrue(status["signed_in"])
        self.assertNotIn("rt-секрет", str(status))


class TestNoClientSecretInCode(unittest.TestCase):
    def test_oauth_module_has_no_client_secret_key(self):
        source = Path(oauth.__file__).read_text(encoding="utf-8")
        # Ключа client_secret в запросах нет: цикл собран только на PKCE.
        self.assertNotIn('"client_secret"', source)