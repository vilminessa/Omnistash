"""OAuth для YouTube Data API: «Desktop/installed app» + PKCE + loopback (G2).

Почему без client_secret и почему это правильный путь для installed-app:
секрет клиента в код и в собранный exe вшивать бессмысленно — exe на машине
человека читается, секрет всё равно становится доступен. Поэтому публичный
client_id + PKCE (RFC 8252, «desktop app» из спецификации OAuth 2.0):
одноразовый код-верификатор code_verifier живёт только в этом процессе и на
loopback-сервере, а площадка хранит code_challenge S256. Обмен кода на токен
возможен только тому процессу, что начал вход: чужая машина, перехватившая
возврат, verifier не знает. client_secret здесь нет ни в коде, ни в сборке,
ни в запросах — это и есть защита installed-app.

client_id — не секрет (он публичный), но в журнал и участникам сети он не
уходит: журнал видит только фазы входа и счётчики.

Цикл входа:
  1. loopback-сервер на 127.0.0.1 с ephemeral port (наружу ничего не
   слушает — redirect_uri вида «http://127.0.0.1:<порт>/», для installed-app
   Google принимает loopback со случайным портом; в консоли client_id
   прописывают redirect этого вида);
  2. authorize-URL с code_challenge (S256), access_type=offline (нужен
   refresh-токен, чтобы пережить перезапуск);
  3. человек подтверждает согласие в браузере; браузер возвращает код на
   loopback; state сверяется (защита от путаницы между запросами);
  4. exchange: code + verifier → refresh-токен;
  5. refresh-токен в google_tokens.bin тем же шифрованием DPAPI, что куки
   аккаунта (app/google_auth.py): файл бесполезен на другой учётке Windows.

Окно от отказа не падает: каждая причина — FlowError с kind и честной
подсказкой, она идёт в state и в журнал; при отказе refresh удаляется.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import google_auth
from .paths import google_tokens_path
from .util import clean_text, now_iso, to_text

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
# Только чтение: метаданные и списки канала. Никаких «записать/удалить».
YOUTUBE_READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
DEFAULT_TIMEOUT = 300.0     # секунд ожидания возврата из браузера
TOKEN_TIMEOUT = 20.0        # секунд на запрос к endpoint токенов


class FlowError(Exception):
    """Причина отказа входа. kind — ветвление подсказок UI (окно не падает):
      unsupported - client_id не настроен/не принят;
      network     - браузер/сеть не ответили при обмене;
      blocked     - OAuth endpoint ответил HTTP-ошибку;
      cancelled   - человек отказал в consent или возврат не дошёл за таймаут;
      invalid_grant - код/refresh не принят (нужен повторный вход);
      empty       - ответ без токенов.
    """

    def __init__(self, message: str, kind: str = "network") -> None:
        super().__init__(message)
        self.kind = kind


def pkce_pair() -> tuple[str, str]:
    """(code_verifier, code_challenge): verifier — 43..128 символов из
    base64url-алфавита, challenge — SHA-256 от verifier в base64url без
    padding (метод S256 — единственный для installed-app в RFC 8252)."""
    verifier = secrets.token_urlsafe(64)          # ~86 символов, в границах RFC
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def authorize_url(client_id: str, redirect_uri: str, state: str,
                  code_challenge: str, scope: str = YOUTUBE_READONLY_SCOPE) -> str:
    """URL consent-страницы: response_type=code, access_type=offline (чтобы
    площадка выдала refresh-токен), PKCE S256, loopback redirect. Здесь нет
    ни client_secret, ни email/password — только публичный client_id."""
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
    }
    return f"{AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"


# --------------------------------------------------------------------------- #
#  Loopback-сервер: слушает 127.0.0.1, принимает только redirect с кодом.
# --------------------------------------------------------------------------- #

class _LoopbackHandler(BaseHTTPRequestHandler):
    """GET на redirect: code + state. Прочие запросы (favicon, ping браузером
    страницы) молча игнорируются: они не должны выставить «вход выполнен»."""

    def do_GET(self) -> None:  # noqa: N002 - имя метода стандартного сервера
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        code = params.get("code", [""])[0]
        error = params.get("error", [""])[0]
        state = params.get("state", [""])[0]
        if not code and not error:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        mismatch = bool(self.server.expected_state) and state != self.server.expected_state
        if mismatch:
            self.server.result["state_mismatch"] = True
        elif code:
            self.server.result["code"] = code
        if error:
            self.server.result["error"] = error
        self._page(mismatch=mismatch, error=error)
        self.server.done.set()

    def _page(self, *, mismatch: bool, error: str) -> None:
        if mismatch:
            text = "Состояние входа не совпало: повторите вход заново."
        elif error:
            text = (f"Площадка не выполнила вход: "
                    f"{urllib.parse.quote(clean_text(error))} "
                    "(вкладку можно закрыть)")
        else:
            text = "Omnistash получил код входа. Вкладку можно закрыть."
        body = (f"<html><head><meta charset='utf-8'></head>"
                f"<body>{text}</body></html>").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass                      # молчание: stderr сервиса не засоряем


def _start_loopback(expected_state: str) -> ThreadingHTTPServer:
    """Сервер на 127.0.0.1 с ephemeral port (наружу не слушает ничего)."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LoopbackHandler)
    server.daemon_threads = True
    server.expected_state = expected_state
    server.result = {}
    server.done = threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name="omnistash-oauth-loopback")
    thread.start()
    return server


def _shutdown(server: ThreadingHTTPServer) -> None:
    try:
        server.shutdown()
    except Exception:  # noqa: BLE001 - сервер мог быть уже закрыт
        pass
    try:
        server.server_close()
    except Exception:  # noqa: BLE001
        pass


def _callback_hint(error: str) -> str:
    """Честная подсказка по error из возврата consent-страницы."""
    if error == "access_denied":
        return ("Человек отказал в подтверждении согласия на consent-странице "
                "(access_denied) — вход можно повторить")
    if error == "invalid_request":
        return ("Площадка отвергла redirect (invalid_request): для публичного "
                "client_id в OAuth-консоли должен быть loopback-redirect "
                "«http://127.0.0.1:<порт>/» (installed app)")
    return f"Площадка вернула ошибку «{error}»"


# --------------------------------------------------------------------------- #
#  Exchange и refresh: POST на OAuth endpoint. Никакого client_secret в теле.
# --------------------------------------------------------------------------- #

def _token_http_error(exc: urllib.error.HTTPError) -> FlowError:
    """HTTP-ошибка endpoint → FlowError с честной подсказкой (тело читаем)."""
    detail = ""
    try:
        payload = json.loads(exc.read().decode("utf-8", "replace"))
        detail = str(payload.get("error") or "")
    except Exception:  # noqa: BLE001 - тело могло быть не-JSON
        detail = clean_text(exc)
    if detail == "invalid_grant":
        return FlowError("Одноразовый код или refresh-токен не принят "
                         "(invalid_grant): повторите вход заново",
                         kind="invalid_grant")
    if detail == "invalid_client" or exc.code in (401, 403):
        return FlowError(f"OAuth endpoint не принял client_id (HTTP {exc.code}): "
                         f"{detail or clean_text(exc)} — проверьте настройку "
                         "«OAuth client_id» (нужен публичный client_id "
                         "installed app без секрета)", kind="blocked")
    return FlowError(f"OAuth endpoint не принял запрос (HTTP {exc.code}): "
                     f"{detail or clean_text(exc)}", kind="blocked")


def _post(url: str, body: dict, *, timeout: float = TOKEN_TIMEOUT) -> dict:
    """POST формы на endpoint OAuth; JSON-ответ. client_secret в теле нет:
    публичный client_id + PKCE — единственный путь installed-app."""
    payload = urllib.parse.urlencode(body).encode("utf-8")
    request = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:       # subclass OSError — ветка раньше
        raise _token_http_error(exc) from exc
    except OSError as exc:
        raise FlowError(f"OAuth endpoint не ответил: {clean_text(exc)}",
                        kind="network") from exc


def exchange_code(client_id: str, code: str, redirect_uri: str,
                  code_verifier: str) -> dict:
    """Обмен одноразового кода на токены (PKCE-верификатор — наш, чужой
    перехват его не повторит). Выдаёт {"refresh_token", "access_token", "scope"}."""
    if not code:
        raise FlowError("Площадка не вернула одноразовый код", kind="empty")
    body = {"grant_type": "authorization_code", "code": code,
            "client_id": client_id, "redirect_uri": redirect_uri,
            "code_verifier": code_verifier}
    tokens = _post(TOKEN_ENDPOINT, body)
    refresh = to_text(tokens.get("refresh_token"))
    if not refresh:
        raise FlowError("OAuth не выдал refresh-токен (площадка не приняла "
                        "access_type=offline): повторите вход", kind="empty")
    return {"refresh_token": refresh,
            "access_token": to_text(tokens.get("access_token")),
            "scope": to_text(tokens.get("scope"))}


def refresh_access_token(client_id: str, refresh_token: str) -> str:
    """Актуальный access-токен по сохранённому refresh (installed-app: client_id
    в запросе есть, client_secret — нет)."""
    tokens = _post(TOKEN_ENDPOINT, {"grant_type": "refresh_token",
                                    "client_id": client_id,
                                    "refresh_token": refresh_token})
    access = to_text(tokens.get("access_token"))
    if not access:
        raise FlowError("OAuth не выдал access-токен", kind="empty")
    return access


# --------------------------------------------------------------------------- #
#  Хранение: google_tokens.bin (DPAPI — то же шифрование, что куки аккаунта)
# --------------------------------------------------------------------------- #

def save_tokens(refresh_token: str, *, scope: str, client_id: str) -> dict:
    """refresh-токен в профиль шифрованием DPAPI (см. google_auth.encrypt).
    Значения токенов не пишутся ни в журнал, ни в настройки, ни наружу."""
    payload = {"refresh_token": refresh_token, "scope": scope,
               "client_id": client_id, "since": now_iso()}
    blob = google_auth.encrypt(json.dumps(payload, ensure_ascii=False)
                                .encode("utf-8"))
    path = google_tokens_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(blob)
    os.replace(tmp, path)
    return {"since": payload["since"], "scope": scope}


def load_tokens() -> dict | None:
    """Сохранённый вход: None — файла нет; {"broken": True} — файл не читается
    (не наша учётка Windows или повреждение): секреты оттуда не достать."""
    path = google_tokens_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(google_auth.decrypt(path.read_bytes())
                          .decode("utf-8"))
    except (OSError, ValueError):
        return {"broken": True}
    if not isinstance(data, dict):
        return {"broken": True}
    return {"broken": False,
            "refresh_token": to_text(data.get("refresh_token")),
            "scope": to_text(data.get("scope")),
            "client_id": to_text(data.get("client_id")),
            "since": to_text(data.get("since"))}


def forget() -> dict:
    """Удалить сохранённые токены («Забыть вход» и отказ invalid_grant)."""
    path = google_tokens_path()
    removed = False
    try:
        path.unlink()
        removed = True
    except FileNotFoundError:
        pass
    return {"removed": removed}


def access_token(settings: dict) -> str | None:
    """Актуальный access-токен по сохранённому refresh; None — входа нет.
    invalid_grant: сохранённый токен удаляем — повтор его каждый раз молча
    гнил бы, а честнее «войдите заново»."""
    tokens = load_tokens()
    if not tokens:
        return None
    if tokens.get("broken"):
        raise FlowError("Сохранённые токены не читаются (не наша учётка): "
                        "войдите заново", kind="unsupported")
    if not tokens.get("refresh_token"):
        return None
    client_id = to_text(settings.get("oauth_client_id")) or tokens.get("client_id")
    if not client_id:
        raise FlowError("OAuth client_id не задан (настройка «OAuth client_id»)",
                        kind="unsupported")
    try:
        return refresh_access_token(client_id, tokens["refresh_token"])
    except FlowError as exc:
        if exc.kind == "invalid_grant":
            forget()
            raise FlowError("Площадка не приняла сохранённый refresh-токен: "
                            "вход удалён из профиля, войдите заново",
                            kind="invalid_grant") from exc
        raise


def status(settings: dict) -> dict:
    """Состояние для UI — без значений секретов: configured (client_id задан),
    signed_in (refresh сохранён), scope, since, честная подсказка hint."""
    client_id = to_text(settings.get("oauth_client_id"))
    tokens = load_tokens()
    broken = bool(tokens and tokens.get("broken"))
    signed_in = bool(tokens and not broken and tokens.get("refresh_token"))
    hint = ""
    if not client_id:
        hint = ("Не задано OAuth client_id (настройка «OAuth client_id»): "
                "вход не настроен")
    elif broken:
        hint = ("Сохранённые токены не читаются (не наша учётка Windows): "
                "войдите заново")
    elif signed_in:
        hint = (f"Вход выполнен (scope {tokens.get('scope') or '?'})"
                f", сохранён {tokens.get('since') or '?'}")
    else:
        hint = "Вход не выполнен: «Войти (OAuth)»"
    return {"configured": bool(client_id), "signed_in": signed_in,
            "scope": tokens.get("scope") if tokens else None,
            "since": tokens.get("since") if tokens else None, "hint": hint}


# --------------------------------------------------------------------------- #
#  Полный цикл входа (worker-поток: окно не ждёт и не падает при отказе).
# --------------------------------------------------------------------------- #

def run_flow(client_id: str, *, state: dict | None = None, opener=None,
             timeout: float = DEFAULT_TIMEOUT, log=None,
             scope: str = YOUTUBE_READONLY_SCOPE) -> dict:
    """Вход installed-app: consent → loopback → exchange → refresh в профиле.
    Ничего не бросает наружу: отказ — {"ok": False, "error": честная подсказка,
    "kind"} и те же поля в state (UI опрашивает phase). В журнал идут только
    фазы: client_id и значения токенов в журнале не появляются."""
    state = state if state is not None else {}
    client_id = to_text(client_id) or ""
    if not client_id:
        state.update({"phase": "error",
                      "error": "OAuth client_id не задан (настройка «OAuth client_id»)"})
        return {"ok": False, "error": state["error"], "kind": "unsupported"}

    verifier, challenge = pkce_pair()
    expected_state = secrets.token_urlsafe(24)
    server = _start_loopback(expected_state)
    redirect = f"http://127.0.0.1:{server.server_port}/"
    url = authorize_url(client_id, redirect_uri=redirect, state=expected_state,
                        code_challenge=challenge, scope=scope)
    state.update({"phase": "waiting", "url": url, "error": None,
                  "reason": "Открыто окно consent: ждём возврат на loopback"})
    try:
        try:
            (opener or webbrowser.open)(url)
        except Exception as exc:  # noqa: BLE001 - браузер мог быть не готов
            raise FlowError(f"Браузер не открылся: {clean_text(exc)}",
                            kind="network") from exc
        if not server.done.wait(timeout):
            raise FlowError(f"Вход не вернулся за {int(timeout)} секунд: "
                            "браузер не дошёл до consent или возврат не "
                            "состоялся — повторите вход", kind="cancelled")
        result = dict(server.result)
        if result.get("state_mismatch"):
            raise FlowError("Площадка вернула не тот state (защита от путаницы "
                            "между запросами): вход отклонён, повторите",
                            kind="cancelled")
        if result.get("error"):
            raise FlowError(_callback_hint(result["error"]), kind="cancelled")
        if not result.get("code"):
            raise FlowError("В возврате нет одноразового кода: повторите вход",
                            kind="empty")
        state.update({"phase": "exchanging",
                      "reason": "Обмен кода на токены (PKCE)"})
        if log:
            log("OAuth: код получен, обмен на токены (PKCE)...")
        tokens = exchange_code(client_id, result["code"], redirect, verifier)
        saved = save_tokens(tokens["refresh_token"],
                            scope=tokens.get("scope") or scope,
                            client_id=client_id)
        state.update({"phase": "done", "error": None, "since": saved["since"],
                      "reason": "Вход выполнен: refresh-токен сохранён"})
        return {"ok": True, "scope": saved["scope"], "since": saved["since"]}
    except FlowError as exc:
        state.update({"phase": "error", "error": str(exc), "kind": exc.kind})
        return {"ok": False, "error": str(exc), "kind": exc.kind}
    finally:
        _shutdown(server)
