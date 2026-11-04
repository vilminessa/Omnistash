"""Привязка клиента по короткому коду (docs/SERVER-CLIENT.md §5, шаг S6).

Код показывает окно сервера, человек его передаёт клиенту, клиент вводит.
Рендер - тот же приём, что app/mesh/sas.py: значение выводится равномерно из
digest (6 цифр или 4 слова из списка SAS_WORDS), сравнение
hmac.compare_digest (перебор не меряется по времени ответа).

Короткий код - это право, которое человек показал ОДНОму клиенту, поэтому
код одноразовый и живёт короткое время: второй клиент тем же кодом
не привязывается, истёкший код назван причиной, а не «неверный код».

В памяти держится только SHA-256 от кода: значение кода не лежит в
реестре и не попадает в журнал аудита (журнал видит метку выданного ключа и
число неверных попыток).

Тормоз перебора: MAX_ATTEMPTS неверных попыток с одного адреса -> задержка,
которая растёт с каждой следующей попыткой; задержка называется подписью,
перебор не превращается в «тихо пробуем дальше».
"""

from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import time

from .. import i18n
from ..mesh.sas import SAS_WORDS
from .scopes import SCOPES, normalize_scopes
from .tokens import TokenStore, TokenStoreError

MAX_ATTEMPTS = 5            # неверных попыток с адреса до задержки
BASE_DELAY = 30.0           # первая задержка, секунд
MAX_DELAY = 600.0           # потолок задержки (перебор не бесконечен)
MAX_CODES = 3               # сколько кодов показываем одновременно
CODE_MINUTES_DEFAULT = 3    # срок жизни кода (настройка serve_pair_minutes)

# Пресеты прав, которые выдаёт привязка: клиент получает не «всё подряд»,
# а то, что человек выбрал в настройках сервера. `desktop` в списке нет:
# нативные цели по HTTP не выполняются вовсе, право на них ничего не даёт
# удалённому клиенту (§4).
SCOPE_PRESETS: dict[str, tuple] = {
    "viewer": ("read", "watch"),
    "editor": ("read", "watch", "queue", "mark"),
    "operator": tuple(scope for scope in SCOPES if scope != "desktop"),
}
PRESET_LABELS = {"viewer": "читатель и плеер",
                 "editor": "читатель, закачка и пометки",
                 "operator": "оператор: всё, кроме нативного окна"}


def preset_scopes(name) -> tuple:
    preset = str(name or "viewer").strip().lower()
    if preset not in SCOPE_PRESETS:
        raise ValueError(f"неизвестный пресет прав привязки: {preset}")
    return normalize_scopes(SCOPE_PRESETS[preset])


def _digest(code: str) -> bytes:
    return hashlib.sha256(str(code).encode("utf-8")).digest()


def code_digits(digest: bytes) -> str:
    """6 цифр: первые 4 байта равномерно в [0, 1_000_000) (приём sas.digits)."""
    whole = 1_000_000
    scaled = int.from_bytes(digest[:4], "big") * whole
    return f"{scaled >> 32:06d}"


def code_words(digest: bytes) -> str:
    """4 слова по 10 бит из первых 40 бит digest того же списка SAS_WORDS."""
    value = int.from_bytes(digest[:5], "big")
    return " ".join(SAS_WORDS[(value >> (30 - 10 * i)) & 0x3FF] for i in range(4))


def render_code(fmt: str, digest: bytes) -> str:
    if fmt == "digits":
        return code_digits(digest)
    if fmt == "words":
        return code_words(digest)
    raise ValueError(f"неизвестный формат кода привязки: {fmt!r}")


def normalize_code(value) -> str:
    """Код, введённый человеком: пробелы и регистр не значат ничего."""
    return " ".join(str(value or "").strip().lower().split())


class Pairing:
    """Активные коды привязки, попытки перебора и выдача ключей."""

    def __init__(self, tokens: TokenStore, log=None, max_codes: int = MAX_CODES):
        self._tokens = tokens
        self._log = log or (lambda message: None)
        self._max_codes = int(max_codes)
        self._codes: list[dict] = []      # {digest, scopes, expires}
        self._used: list[bytes] = []      # снятые коды: повтор назван причиной
        self._attempts: dict[str, int] = {}
        self._last_wrong: dict[str, float] = {}

    # -- коды ---------------------------------------------------------------

    def begin(self, scopes, minutes: float = CODE_MINUTES_DEFAULT,
              fmt: str = "digits") -> dict:
        """Показать новый код. Предыдущие active-коды снимаются: человек
        показывает один код одному клиенту, а не горстку.
        """
        granted = preset_scopes(scopes) if isinstance(scopes, str) \
            else normalize_scopes(scopes)
        minutes = max(0.2, float(minutes or CODE_MINUTES_DEFAULT))
        secret = secrets.token_urlsafe(24)
        digest = _digest(secret)
        self._prune()
        if len(self._codes) >= self._max_codes:
            self._codes = self._codes[-self._max_codes + 1:]
        code = render_code(fmt, digest)
        expires = time.time() + minutes * 60.0
        self._codes.append({"digest": _digest(code), "scopes": granted,
                            "expires": expires})
        self._log(f"Привязка: код показан (права: {'+'.join(granted)}, "
                  f"действует {minutes:.0f} мин, одноразовый)")
        return {"ok": True, "code": code, "format": fmt, "scopes": list(granted),
                "preset_label": PRESET_LABELS.get(str(scopes), ""),
                "expires_at": expires, "minutes": minutes}

    def state(self) -> dict:
        """Сколько кодов активно (значений кодов здесь нет)."""
        self._prune()
        return {"active": len(self._codes),
                "expires_at": max((entry["expires"] for entry in self._codes),
                                  default=0.0),
                "scopes": list(self._codes[-1]["scopes"]) if self._codes else []}

    def _prune(self) -> None:
        now = time.time()
        self._codes = [entry for entry in self._codes if entry["expires"] > now]

    def delay_for(self, addr: str) -> float:
        """Задержка после MAX_ATTEMPTS неверных попыток с адреса (растёт)."""
        tries = self._attempts.get(str(addr), 0)
        if tries < MAX_ATTEMPTS:
            return 0.0
        extra = tries - MAX_ATTEMPTS
        return min(MAX_DELAY, BASE_DELAY * (2 ** extra))

    def blocked(self, addr: str) -> float:
        """Сколько секунд ещё ждать (0 - задержки нет)."""
        delay = self.delay_for(addr)
        if delay <= 0:
            return 0.0
        since = time.time() - self._last_wrong.get(str(addr), 0.0)
        return max(0.0, delay - since)

    def _count_attempt(self, addr: str) -> None:
        """Записать неверную попытку с адреса и, если лимит пройден, сказать о тормозе."""
        tries = self._attempts.get(str(addr), 0) + 1
        self._attempts[str(addr)] = tries
        self._last_wrong[str(addr)] = time.time()
        if self.delay_for(addr) > 0:
            self._log(f"Привязка: с {addr} неверных попыток {tries} - "
                      f"перебор затормозен на {int(self.delay_for(addr))} с")

    # -- выдача ключа -------------------------------------------------------

    def consume(self, code, addr: str, label: str = "") -> dict:
        """Клиент ввёл код: выдать токен со скоупами кода.

        Причина отказа называется всегда: «код не принят», «код истёк»,
        «сервер не показывал код», «задержка после перебора», «лимит
        ключей» - молчания и выдуманного успеха нет.
        """
        wait = self.blocked(addr)
        if wait > 0:
            return {"error": i18n.t("http.pair_throttle", "ru") +
                    f" ({int(wait)} с)", "code": "throttle", "retry_in": int(wait)}
        self._prune()
        needle = normalize_code(code)
        if not needle:
            return {"error": i18n.t("http.pair_empty", "ru"), "code": "pairing"}
        digest = _digest(needle)
        if any(hmac.compare_digest(entry, digest) for entry in self._used):
            # Код был показан и уже снят: «не принят» было бы молчанием о том,
            # что право уже выдано кому-то другим.
            self._count_attempt(addr)
            return {"error": i18n.t("http.pair_used", "ru"), "code": "pairing"}
        if not self._codes:
            return {"error": i18n.t("http.pair_no_code", "ru"), "code": "pairing"}
        match = None
        for entry in self._codes:
            if hmac.compare_digest(entry["digest"], digest):
                match = entry
                break
        if match is None:
            self._count_attempt(addr)
            return {"error": i18n.t("http.pair_denied", "ru"), "code": "pairing"}
        self._codes.remove(match)          # одноразовый: повторно не пройдёт
        self._used.append(match["digest"])
        self._attempts.pop(str(addr), None)
        self._last_wrong.pop(str(addr), None)
        try:
            token = self._tokens.issue(match["scopes"],
                                       label or f"клиент {'+'.join(match['scopes'])}")
        except (TokenStoreError, ValueError) as exc:
            # Код снят, ключ не выдан: человек увидит причину и покажет новый код.
            return {"error": str(exc), "code": "tokens"}
        self._log(f"Привязка: ключ выдан ({token.label}, права "
                  f"{'+'.join(token.scopes)}) от {addr}")
        return {"ok": True, "paired": True, "token": token.value,
                "label": token.label, "scopes": list(token.scopes),
                "created_at": token.created_at}

    def attempts(self, addr: str) -> int:
        return int(self._attempts.get(str(addr), 0))

    def attempt_total(self) -> int:
        """Сумма неверных попыток со всех адресов (адреса здесь не показаны)."""
        return sum(int(value or 0) for value in self._attempts.values())
