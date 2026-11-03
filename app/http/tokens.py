"""Токены доступа HTTP-клиента: право, зашифрованное DPAPI.

Файл `serve_tokens.bin` в профиле шифруется тем же способом, что куки
аккаунта (app/google_auth.encrypt): на другой машине и для другой учётки
Windows он бесполезен, и на диске не лежит plaintext (docs/SERVER-CLIENT.md §5).

Токен - случайные 32 байта (secrets.token_urlsafe): перебор не меряется по
времени ответа (hmac.compare_digest). В файл и в журнал не пишутся пути и
значения токенов: аудит видит метку токена («read+watch»), а не секрет.
"""

from __future__ import annotations

import hmac
import json
import secrets
import time
from pathlib import Path

from .. import i18n
from ..google_auth import decrypt, encrypt
from ..paths import serve_tokens_path
from .scopes import normalize_scopes

MAX_TOKENS = 8        # сколько ключей живёт одновременно (перебор ограничен)


class TokenStoreError(Exception):
    """Хранилище токенов не работает: DPAPI не ответил, файл битый, лимит исчерпан."""


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class Token:
    """Запись токена: значение известно только серверу, метка - человеку."""

    __slots__ = ("id", "label", "scopes", "created_at", "value")

    def __init__(self, id: str, label: str, scopes: tuple,
                 created_at: str, value: str) -> None:
        self.id = id
        self.label = label
        self.scopes = scopes
        self.created_at = created_at
        self.value = value

    def public(self) -> dict:
        """Что показывает человек в списке ключей (без значения токена)."""
        return {"id": self.id, "label": self.label, "scopes": list(self.scopes),
                "created_at": self.created_at}


class TokenStore:
    """Реестр токенов в профиле (DPAPI). Файл читается и пишется целиком."""

    def __init__(self, path: Path | None = None, max_tokens: int = MAX_TOKENS):
        self._path = Path(path) if path else serve_tokens_path()
        self._max = int(max_tokens)
        self._tokens: dict[str, Token] = {}
        self._dirty = False
        self._loaded_at = 0
        self._load()

    # -- хранение ------------------------------------------------------------

    def _mtime(self) -> int:
        """Момент изменения файла реестра (0 - файла ещё нет)."""
        try:
            return self._path.stat().st_mtime_ns
        except OSError:
            return 0

    def _reload_if_changed(self) -> None:
        """Перечитать реестр, если файл изменился, пока слушатель живёт.

        Ключи выдаёт pairing (S6) и отзывает человек из карточки: оба пишут
        тот же файл. Реестр, прочитанный один раз при старте, показал бы
        «ключа нет» для freshly выданного токена - это молчание о праве,
        которого уже есть.
        """
        mtime = self._mtime()
        if mtime != self._loaded_at:
            self._load()
            self._loaded_at = mtime

    def _read_raw(self) -> bytes | None:
        try:
            return self._path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise TokenStoreError(f"файл токенов не читается: {exc}") from exc

    def _load(self) -> None:
        raw = self._read_raw()
        self._loaded_at = self._mtime()
        if not raw:
            self._tokens = {}
            return
        try:
            data = json.loads(decrypt(raw).decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - DPAPI или битый JSON
            # Чужой файл (другая учётка) или правка руками: не молчим -
            # сервер не поднимется с неизвестными правами.
            raise TokenStoreError(
                i18n.t("http.tokens_broken", "ru")
                + f": {type(exc).__name__}") from exc
        records = data.get("tokens") or []
        self._tokens = {}
        for record in records:
            try:
                scopes = normalize_scopes(record.get("scopes"))
            except ValueError as exc:
                raise TokenStoreError(str(exc)) from exc
            token = Token(str(record.get("id") or ""),
                          str(record.get("label") or ""), scopes,
                          str(record.get("created_at") or ""),
                          str(record.get("value") or ""))
            if token.value:
                self._tokens[token.value] = token

    def _save(self) -> None:
        if not self._dirty:
            return
        payload = json.dumps({"tokens": [
            {"id": token.id, "label": token.label, "scopes": list(token.scopes),
             "created_at": token.created_at, "value": token.value}
            for token in sorted(self._tokens.values(), key=lambda t: t.created_at)
        ]}, ensure_ascii=False).encode("utf-8")
        try:
            sealed = encrypt(payload)
        except OSError as exc:
            raise TokenStoreError(
                i18n.t("http.tokens_dpapi", "ru") + f": {exc}") from exc
        try:
            self._path.write_bytes(sealed)
        except OSError as exc:
            raise TokenStoreError(f"файл токенов не записан: {exc}") from exc
        self._dirty = False
        self._loaded_at = self._mtime()

    # -- права ---------------------------------------------------------------
    # Каждый вход перечитывает реестр, если файл изменился: ключ выдаёт pairing
    # (S6), отзывает человек из карточки, и оба пишут тот же файл.

    def issue(self, scopes, label: str = "") -> Token:
        """Выдать ключ с скоупами. Лимит числа ключей - честный отказ."""
        self._reload_if_changed()
        granted = normalize_scopes(scopes)
        if not granted:
            raise ValueError(i18n.t("http.scopes_none", "ru"))
        if len(self._tokens) >= self._max:
            raise TokenStoreError(
                i18n.t("http.tokens_limit", "ru") + f" ({self._max})")
        token = Token(secrets.token_hex(8), label or "клиент", granted, _now(),
                      secrets.token_urlsafe(32))
        self._tokens[token.value] = token
        self._dirty = True
        self._save()
        return token

    def revoke(self, id: str) -> bool:
        """Отзвать ключ по id. False - такого ключа нет (честно, не молча)."""
        self._reload_if_changed()
        for value, token in list(self._tokens.items()):
            if token.id == id:
                del self._tokens[value]
                self._dirty = True
                self._save()
                return True
        return False

    def verify(self, secret: str) -> Token | None:
        """Найти токен по значению (сравнение без измерения времени).

        compare_digest на строках не принимает не-ASCII, а заголовок
        Authorization приходит из сети: сравниваем байты.
        """
        if not secret:
            return None
        self._reload_if_changed()
        needle = secret.encode("utf-8", errors="surrogatepass")
        for value, token in self._tokens.items():
            if hmac.compare_digest(value.encode("utf-8"), needle):
                return token
        return None

    def list(self) -> list[dict]:
        """Записи для человека: метки и скоупы, значений токенов нет."""
        self._reload_if_changed()
        return [token.public() for token in
                sorted(self._tokens.values(), key=lambda t: t.created_at)]

    def count(self) -> int:
        self._reload_if_changed()
        return len(self._tokens)
