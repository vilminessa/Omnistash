"""Мелкие помощники: время, числа, даты, имена файлов.

Здесь только то, что нужно нескольким модулям и что не относится к
какой-то одной задаче. Главное правило модуля: наружу отдаём
нормализованные значения (дата -> "YYYY-MM-DD", размер -> байты int),
а не сырые строки площадки.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime

# Запрещённые в Windows символы и управляющие коды.
_ILLEGAL_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Имена, зарезервированные системой (с расширением тоже нельзя).
_RESERVED = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
# "20250101", "2025-01-01", "2025-01-01T12:00:00Z" - год в начале обязательно.
_DATE_RE = re.compile(r"^(\d{4})-?(\d{2})-?(\d{2})")


def now_iso() -> str:
    """Момент сейчас в ISO-8601 (секунды, без микросекунд).

    Строковое представление выбрано намеренно: сортируется как текст,
    переживает переезд БД между машинами и не зависит от часового пояса
    в колонке - весь UI показывает локальное время.
    """
    return datetime.now().replace(microsecond=0).isoformat()


def iso_date(value) -> str | None:
    """Дата площадки -> "YYYY-MM-DD" или None.

    yt-dlp отдаёт upload_date как "20250101", в плоских записях плейлиста
    встречается уже готовый ISO. Всё, что не распознано, - None: лучше
    пустая дата, чем мусор, из-за которого ломается сортировка и шаблоны.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = _DATE_RE.match(text)
    if not match:
        return None
    year, month, day = match.groups()
    try:
        datetime(int(year), int(month), int(day))
    except ValueError:
        return None
    return f"{year}-{month}-{day}"


def to_int(value, default: int | None = None) -> int | None:
    """Число из чего угодно; мусор -> default.

    Пустая строка и None - это «нет значения» (None), а не 0: нулевая
    длительность и «длительности нет» - разные вещи.
    """
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def to_text(value) -> str | None:
    """Строка или None: пустое и отсутствующее сводятся к None.

    В БД не должно быть "" - иначе «пусто» и «не заполнено» неразличимы
    при запросах вроде WHERE title IS NOT NULL.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def human_size(size) -> str:
    """Байты -> «1.4 ГиБ» (бинарные единицы: так считают диски и файловые системы)."""
    try:
        left = float(size)
    except (TypeError, ValueError):
        return "-"
    units = ("Б", "КиБ", "МиБ", "ГиБ", "ТиБ", "ПиБ")
    index = 0
    while left >= 1024 and index < len(units) - 1:
        left /= 1024
        index += 1
    if index == 0:
        return f"{int(left)} {units[0]}"
    return f"{left:.1f} {units[index]}"


def human_duration(seconds) -> str:
    """Секунды -> «1:23:45» (для таблицы библиотеки)."""
    total = to_int(seconds)
    if total is None or total < 0:
        return "-"
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def sanitize_name(name: str, limit: int = 150) -> str:
    """Имя файла/папки, безопасное для Windows.

    NFC-нормализация (иначе один и тот же текст с разными юникод-последовательностями
    даёт два пути), запрещённые символы -> «_», хвостовые точки и пробелы
    срезаются (Windows их молча убирает), зарезервированные имена дополняются.
    """
    text = unicodedata.normalize("NFC", str(name or ""))
    text = _ILLEGAL_RE.sub("_", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.rstrip(". ")
    if not text:
        return "_"
    stem = text.split(".")[0].upper()
    if stem in _RESERVED:
        text = "_" + text
    if len(text) > limit:
        text = text[:limit].rstrip(". ")
    return text or "_"


def norm_title(title) -> str:
    """Ключ сравнения названий для матча файлов: без регистра и лишних пробелов."""
    text = unicodedata.normalize("NFC", str(title or "")).casefold()
    return re.sub(r"\s+", " ", text).strip()
