"""SAS - сверка «12 слов» для дружбы: канон, форматы, обёртки (1.1).

Канон - HKDF-SHA256(эфемерный X25519-ECDH, соль версии, info =
сортированные Ed25519-идентичности пары) -> 32 байта; наружу SAS не
передаётся НИКОГДА - только рендерится каждому своему экрану. MITM
подменяет эфемер -> shared разный -> сверка разойдётся; другой паре
устройств и другой версии протокола - другие байты (привязка через
salt/info).

Формат сверки выбирает ИНИЦИАТОР дружбы:
  * words  - 12 русских слов (словарь 1024 из sas_words.py, 10 бит на
             позицию, MSB-first из первых 120 бит);
  * emoji  - 8 эмодзи из 64 (байты 0..5);
  * icons  - 8 генеративных SVG-пиктограмм (8 форм x 8 цветов, байты
             6..11 - свой срез, не чужие биты);
  * digits - 8 цифр (первые 4 байта -> равномерно [0, 10^8)).

Канон и словарь - стабильные данные: тестовые векторы в
tests/test_sas.py фиксируют стабильность (нельзя менять нарезку,
словарь и списки без смены версии протокола).

Исходник канона - наработки vilmnote (a3fa9c7), решение по словарю за
vilmpc: приняты русские 1024 (читабельнее по телефону, чем сгенерённые
cv-cv-cv). Обёртки own_ephemeral/sas_words - API для клиента и сервера
(/sas/).
"""

from __future__ import annotations

import hashlib

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey)
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import (
    Encoding, PublicFormat)

from .sas_words import SAS_WORDS

SAS_VERSION = b"omnistash-sas-v1"
METHODS = ("words", "emoji", "icons", "digits")
METHOD_LABELS = {
    "words": "12 слов",
    "emoji": "эмодзи",
    "icons": "иконки",
    "digits": "цифры",
}

# 64 эмодзи: общеупотребительные, стабильные код-пойнты, без вариаций
# «держатель/цвет/контекст» - на разных ОС рисуются по-разному, но
# СВЕРЯЕТСЯ форма/предмет, как в Matrix SAS.
EMOJI: tuple = (
    "🍎", "🍌", "🍇", "🍉", "🍓", "🍍", "🥝", "🍑",
    "🍒", "🥥", "🥑", "🍆", "🥔", "🥕", "🌽", "🌶",
    "🐱", "🐶", "🦊", "🐻", "🐼", "🐨", "🐯", "🦁",
    "🐸", "🐵", "🦄", "🐙", "🦋", "🐞", "🐢", "🐳",
    "⚽", "🎨", "🎸", "🎈", "🎯", "🚀", "🎪", "🎬",
    "🔑", "⌚", "📷", "💡", "🎁", "🧩", "🎲", "🛎",
    "⭐", "🔥", "💧", "🌈", "🌙", "☀", "⚡", "❄",
    "❤", "✔", "✈", "⛺", "🏁", "🔔", "🏆", "📌",
)


def sas_bytes(shared: bytes, id_a: str, id_b: str) -> bytes:
    """32 байта SAS для пары устройств (наружу не передаётся)."""
    pair = ":".join(sorted((str(id_a).lower(), str(id_b).lower())))
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=SAS_VERSION,
        info=pair.encode("ascii"),
    ).derive(bytes(shared))


def make_eph() -> tuple:
    """Одноразовый эфемерный ключ X25519 для SAS-обмена."""
    priv = X25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return priv, pub.hex()


def shared_with(priv, their_pub_hex: str) -> bytes:
    """Общий секрет ECDH; чужая публичная часть - 32 байта hex."""
    raw = bytes.fromhex(str(their_pub_hex).strip())
    if len(raw) != 32:
        raise ValueError("эфемерный ключ должен быть 32 байта")
    return priv.exchange(X25519PublicKey.from_public_bytes(raw))


def _chunks6(digest: bytes) -> list:
    """6 байт (48 бит) -> 8 индексов по 6 бит (MSB-first)."""
    value = int.from_bytes(digest[:6], "big")
    return [(value >> (42 - 6 * i)) & 0x3F for i in range(8)]


def words(digest: bytes) -> list:
    """12 слов: по 10 бит на позицию (1024 = 2^10, 120 бит <= 32 байта).

    Нарезка MSB-first из первых 120 бит: равномерная, без смещения.
    """
    value = int.from_bytes(digest[:15], "big")
    return [SAS_WORDS[(value >> (110 - 10 * i)) & 0x3FF]
            for i in range(12)]


def emoji(digest: bytes) -> list:
    """8 эмодзи из 64 (байты 0..5)."""
    return [EMOJI[i] for i in _chunks6(digest)]


def icons(digest: bytes) -> list:
    """8 индексов SVG-иконок из 64 (байты 6..11)."""
    shifted = digest[6:12] + digest[:6]     # свой срез, не чужие биты
    return _chunks6(shifted)


def digits(digest: bytes) -> str:
    """8 цифр: первые 4 байта -> равномерно в [0, 100000000)."""
    whole = 100_000_000
    scaled = int.from_bytes(digest[:4], "big") * whole
    return f"{scaled >> 32:08d}"


def render(method: str, digest: bytes):
    """Сверяемая строка/список для выбранного формата."""
    if method == "words":
        return words(digest)
    if method == "emoji":
        return emoji(digest)
    if method == "icons":
        return icons(digest)
    if method == "digits":
        return digits(digest)
    raise ValueError(f"неизвестный формат сверки: {method!r}")


# --- генеративные SVG-иконки: без ассетов, детерминированно ---

_ICON_SHAPES = (
    "circle", "square", "triangle", "star",
    "hexagon", "diamond", "cross", "ring",
)
_ICON_COLORS = (
    "#e5484d", "#f76b15", "#ffc53d", "#46a758",
    "#12a594", "#0091ff", "#8e4ec6", "#8d6e63",
)


def svg_icon(index: int) -> str:
    """SVG-пиктограмма 0..63: форма = index % 8, цвет = index // 8."""
    index = int(index) & 0x3F
    shape, color = _ICON_SHAPES[index % 8], _ICON_COLORS[index // 8]
    body = {
        "circle": '<circle cx="32" cy="32" r="22"/>',
        "square": '<rect x="12" y="12" width="40" height="40" rx="6"/>',
        "triangle": '<polygon points="32,10 54,52 10,52"/>',
        "star": ('<polygon points="32,8 39,26 58,26 43,37 49,56 32,45 '
                 '15,56 21,37 6,26 25,26"/>'),
        "hexagon": ('<polygon points="32,8 52,20 52,44 32,56 12,44 '
                    '12,20"/>'),
        "diamond": '<polygon points="32,8 56,32 32,56 8,32"/>',
        "cross": ('<path d="M20 8 h24 v12 h12 v24 h-12 v12 h-24 v-12 '
                  'h-12 v-24 h12 z"/>'),
        "ring": ('<path d="M32 8 a24 24 0 1 0 0.1 0 z '
                 'M32 20 a12 12 0 1 1 -0.1 0 z" fill-rule="evenodd"/>'),
    }[shape]
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" '
            f'width="64" height="64"><g fill="{color}">{body}</g></svg>')


def validate() -> None:
    """Самопроверка канона (ловит случайную правку словаря/списков)."""
    assert len(SAS_WORDS) == 1024, len(SAS_WORDS)
    assert len(set(SAS_WORDS)) == 1024, "дубли в словаре SAS"
    assert len(EMOJI) == 64 and len(set(EMOJI)) == 64, "эмодзи: 64 шт"
    assert len(_ICON_SHAPES) == 8 and len(_ICON_COLORS) == 8


# ------------------------------------------------------------------ #
#  Обёртки API для /sas/ (клиент sas_begin + серверный обработчик)
# ------------------------------------------------------------------ #

def own_ephemeral() -> tuple:
    """Свежая ephemeral-пара: (ключ, hex публичного). Не сохраняется."""
    return make_eph()


def shared_secret(own_priv, peer_pub_hex: str) -> bytes:
    """Общий секрет ECDH(свой ephemeral, чужой ephemeral)."""
    return shared_with(own_priv, peer_pub_hex)


def sas_render(own_priv, peer_pub_hex: str, own_id_hex: str,
               peer_id_hex: str, method: str = "words"):
    """Сверка для пары ephemeral-ключей и идентичностей устройств.

    digest = sas_bytes(ECDH(...), idA, idB) -> render(method).
    Возвращает строку (digits) или список (words/emoji) или список
    SVG-строк (icons).
    """
    digest = sas_bytes(shared_secret(own_priv, peer_pub_hex),
                       own_id_hex, peer_id_hex)
    if method == "icons":
        return [svg_icon(index) for index in icons(digest)]
    return render(method, digest)


def digest_for(own_priv, peer_pub_hex: str, own_id_hex: str,
               peer_id_hex: str) -> bytes:
    """Сырой 32-байтный SAS-дайджест (для серверного журнала)."""
    return sas_bytes(shared_secret(own_priv, peer_pub_hex),
                     own_id_hex, peer_id_hex)
