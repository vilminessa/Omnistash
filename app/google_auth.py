"""Аккаунт Google: куки для загрузки, токены для API (G1b).

Почему куки, а не «просто токен»: yt-dlp качает потоки как браузер, и
площадка спрашивает именно сессию браузера (возрастной контент, «подтвердите,
что вы не бот»). OAuth-токен нужен для API метаданных и файлы не качает -
поэтому это две независимые штуки, живущие рядом.

Секреты (куки, токены) шифруются DPAPI в пределах текущей Windows-учётки:
копия бесполезна на другой машине и для другого пользователя. Никогда не
попадают в settings.json и в журнал.

Экспорт - Netscape txt (тот формат, что понимает yt-dlp); сам файл
отдаётся качалке расшифрованным во временный путь на время загрузки и
удаляется сразу после.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import http.cookiejar
import os
import tempfile
from pathlib import Path

from .paths import google_cookies_path, google_tokens_path

# Крипто через ctypes: зачем-то тащить dependency, если Windows даёт API.
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes):
    """(DataBlob, буфер) - буфер обязан жить дольше вызова."""
    buffer = ctypes.create_string_buffer(data, len(data))
    blob = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    return blob, buffer


def _crypt32():
    module = ctypes.WinDLL("crypt32", use_last_error=True)
    module.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob), wintypes.LPCWSTR, ctypes.POINTER(_DataBlob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(_DataBlob)]
    module.CryptProtectData.restype = wintypes.BOOL
    # Внимание: расшифровка - ДРУГАЯ функция. Ловушка, в которую я уже
    # попал: вызов CryptProtectData вместо CryptUnprotectData «успешно»
    # шифрует второй раз и раундтрип выдаёт мусор.
    module.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.POINTER(_DataBlob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(_DataBlob)]
    module.CryptUnprotectData.restype = wintypes.BOOL
    return module


def _kernel32():
    module = ctypes.WinDLL("kernel32", use_last_error=True)
    module.LocalFree.argtypes = [ctypes.c_void_p]
    module.LocalFree.restype = ctypes.c_void_p
    return module


def encrypt(data: bytes) -> bytes:
    """Зашифровать DPAPI (user scope): ключ - текущая Windows-учётка."""
    source, keep = _blob(data)
    out = _DataBlob()
    if not _crypt32().CryptProtectData(
            ctypes.byref(source), "Omnistash", None, None, None,
            _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise OSError(f"DPAPI: не удалось зашифровать (код "
                      f"{ctypes.get_last_error()})")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _kernel32().LocalFree(out.pbData)


def decrypt(data: bytes) -> bytes:
    """Обратная операция; чужой файл (другая учётка/машина) падает."""
    source, keep = _blob(data)
    out = _DataBlob()
    if not _crypt32().CryptUnprotectData(
            ctypes.byref(source), None, None, None, None,
            _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise OSError(f"DPAPI: не удалось расшифровать (код "
                      f"{ctypes.get_last_error()})")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _kernel32().LocalFree(out.pbData)


# Домены, которые несёт сессия Google для youtube-загрузок: аккаунтские
# куки живут на .google.com и покрывают youtube.com.
_GOOGLE_SUFFIXES = (".google.com", ".youtube.com", ".googlevideo.com",
                    ".youtu.be")


def is_google_cookie(cookie) -> bool:
    domain = (getattr(cookie, "domain", "") or "").lstrip(".").lower()
    return any(domain == suffix.lstrip(".") or
               domain.endswith(suffix) for suffix in _GOOGLE_SUFFIXES)


def export_netscape(cookies) -> str:
    """Куки (http.cookiejar.Cookie из pywebview) -> Netscape txt для yt-dlp.

    Через временный файл: с3.14 MozillaCookieJar работает только с путями
    (пишет с правами600) и file-object больше не принимает.
    """
    jar = http.cookiejar.MozillaCookieJar()
    for cookie in cookies:
        jar.set_cookie(cookie)
    handle = tempfile.NamedTemporaryFile(
        prefix="omnistash-netscape-", suffix=".txt", delete=False)
    handle.close()
    try:
        jar.save(handle.name, ignore_discard=True, ignore_expires=True)
        return Path(handle.name).read_text(encoding="utf-8")
    finally:
        Path(handle.name).unlink(missing_ok=True)


def save_cookies(cookies, *, min_count: int = 1) -> int:
    """Зашифровать и сохранить копию кук. Возвращает, сколько сохранилось.

    min_count - страховка от «поймали пустую страницу»: не пишем пустой
    или подозрительно маленький набор поверх настоящего.
    """
    picked = [c for c in cookies if is_google_cookie(c)]
    if len(picked) < min_count:
        raise RuntimeError(f"подозрительно мало кук Google: {len(picked)}")
    # Сначала честный экспорт (через временный файл - см. export_netscape),
    # потом шифрование и атомарная подмена.
    text = export_netscape(picked)
    target = google_cookies_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    # Атомарно: буфер рядом с целью, затем подмена.
    tmp = target.with_suffix(".tmp")
    tmp.write_bytes(encrypt(text.encode("utf-8")))
    os.replace(tmp, target)
    return len(picked)


def save_text_cookies(text: str) -> int:
    """Сохранить куки из импортированного Netscape-файла (проверка формата)."""
    if "# Netscape HTTP Cookie File" not in text and "\t" not in text:
        raise RuntimeError("это не похоже на cookies.txt (нет заголовка "
                           "Netscape и табуляций)")
    handle = tempfile.NamedTemporaryFile(
        prefix="omnistash-import-", suffix=".txt", delete=False,
        mode="w", encoding="utf-8")
    handle.write(text)
    handle.close()
    try:
        jar = http.cookiejar.MozillaCookieJar()
        # в3.14 у FileCookieJar только load/save по путям (read убран).
        jar.load(handle.name, ignore_discard=True, ignore_expires=True)
        cookies = list(jar)
    finally:
        Path(handle.name).unlink(missing_ok=True)
    if not cookies:
        raise RuntimeError("в файле нет ни одной куки")
    return save_cookies(cookies)


def has_cookies() -> bool:
    return google_cookies_path().is_file()


def decrypted_cookies_text() -> str | None:
    """Расшифрованная копия (None - аккаунта нет). Текст - секрет."""
    path = google_cookies_path()
    if not path.is_file():
        return None
    return decrypt(path.read_bytes()).decode("utf-8")


def temporary_cookiefile() -> Path | None:
    """Расшифровать куки во временный файл для yt-dlp.

    Путь обязан пройти через release_temp(): куки на диске живут ровно
    столько, сколько идёт загрузка.
    """
    text = decrypted_cookies_text()
    if text is None:
        return None
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", prefix="omnistash-cookies-",
        suffix=".txt", delete=False)
    handle.write(text)
    handle.close()
    return Path(handle.name)


def release_temp(path: Path | None) -> None:
    if path:
        try:
            Path(path).unlink()
        except OSError:
            pass


def forget() -> dict:
    """«Забыть аккаунт»: удалить зашифрованные копии. Токены тоже."""
    removed = []
    for path in (google_cookies_path(), google_tokens_path()):
        try:
            path.unlink()
            removed.append(path.name)
        except FileNotFoundError:
            pass
        except OSError:
            pass
    return {"removed": removed}


def account_state(settings: dict) -> dict:
    """Состояние аккаунта для окна. Никаких секретов - только факты."""
    return {
        "has_cookies": has_cookies(),
        "use_cookies": bool(settings.get("use_google_cookies", True)),
        "label": str(settings.get("google_account_label") or ""),
        "since": str(settings.get("google_account_since") or ""),
    }
