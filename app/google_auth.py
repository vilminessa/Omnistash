"""Аккаунты Google: копии кук для доступа к контенту конкретных источников.

Почему копии, а не «токен»: yt-dlp качает и метаданные как браузер, и
площадка спрашивает сессию браузера (возраст, «подтвердите, что не бот»).
Аккаунт здесь - это ПРИВЯЗКА к источнику: у плейлиста может быть своя
учётка, и её куки используются при синке этого источника и загрузке его
видео. Глобального аккаунта нет - только явные привязки.

Секреты шифруются DPAPI в пределах текущей Windows-учётки: копия
бесполезна на другой машине и для другого пользователя. Ни куки, ни их
значения не попадают в settings.json и в журнал - только имена доменов
и счётчики (диагностика).

Реестр (метки/даты) - accounts.json в профиле; копии кук - accounts/<id>.bin.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import http.cookiejar
import json
import os
import tempfile
import uuid
from pathlib import Path

from .paths import profile_dir

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
    # попал: CryptProtectData вместо CryptUnprotectData «успешно» шифрует
    # второй раз и раундтрип выдаёт мусор.
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


# Домены, которые несёт сессия Google для youtube: аккаунтские куки живут
# на .google.com и покрывают youtube.com.
_GOOGLE_SUFFIXES = (".google.com", ".youtube.com", ".googlevideo.com",
                    ".youtu.be")


def is_google_cookie(cookie) -> bool:
    domain = (getattr(cookie, "domain", "") or "").lstrip(".").lower()
    return any(domain == suffix.lstrip(".") or
               domain.endswith(suffix) for suffix in _GOOGLE_SUFFIXES)


def _morsel_field(morsel, name, default=""):
    """Поле morsel: зарезервированные ключи ('domain', 'path'...) живут как
    ПАРЫ DICT, а не как атрибуты - читаем через get с фолбэком на getattr."""
    getter = getattr(morsel, "get", None)
    if callable(getter):
        value = getter(name, None)
        if value not in (None, ""):
            return value
    return getattr(morsel, name, default)


def as_cookie_list(raw, default_domain: str = "") -> list:
    """window.get_cookies() -> список http.cookiejar.Cookie.

    pywebview отдаёт РАЗНЫЕ форматы в зависимости от состояния окна: и
    готовые Cookie, и list[SimpleCookie] (morsel: value/path/httponly, но
    домен пустой). Воркер падал на втором формате - поэтому здесь вся
    нормализация. Для morsel без домена берём default_domain (хост текущей
    страницы окна): куки честно сняты для этой страницы, в Netscape-файл
    домен обязан попасть.
    """
    if raw is None:
        return []
    if isinstance(raw, http.cookiejar.Cookie):
        return [raw]
    if isinstance(raw, (list, tuple)):
        out = []
        for item in raw:
            out.extend(as_cookie_list(item, default_domain))
        return out
    items = getattr(raw, "items", None)      # SimpleCookie и прочие мапы
    if callable(items):
        out = []
        for name, morsel in list(items())[:200]:
            domain = str(_morsel_field(morsel, "domain") or
                         default_domain or "").strip()
            path = str(_morsel_field(morsel, "path") or "/") or "/"
            try:
                out.append(http.cookiejar.Cookie(
                    version=0, name=str(name),
                    value=str(getattr(morsel, "value", "")),
                    port=None, port_specified=False,
                    domain=domain, domain_specified=bool(domain),
                    domain_initial_dot=domain.startswith("."),
                    path=path, path_specified=True,
                    secure=bool(_morsel_field(morsel, "secure", False)),
                    expires=None, discard=True, comment=None,
                    comment_url=None, rest={"httponly":
                                            _morsel_field(morsel, "httponly", "")},
                    rfc2109=False))
            except Exception:  # noqa: BLE001 - кривая кука не должна ронять
                continue
        return out
    return []


def export_netscape(cookies) -> str:
    """Куки (http.cookiejar.Cookie) -> Netscape txt для yt-dlp.

    Через временный файл: с3.14 MozillaCookieJar работает только с путями.
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


def parse_netscape(text: str) -> list:
    """Netscape txt -> куки (с проверкой формата)."""
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
    return cookies


# --------------------------------------------------------------------------- #
#  Реестр аккаунтов: accounts.json (метки) + accounts/<id>.bin (DPAPI-куки)
# --------------------------------------------------------------------------- #

def accounts_dir() -> Path:
    return profile_dir() / "accounts"


def account_cookies_path(account_id: str) -> Path:
    return accounts_dir() / f"{account_id}.bin"


def _registry_path() -> Path:
    return profile_dir() / "accounts.json"


def load_registry() -> list[dict]:
    """[{id, label, since}] - никаких секретов, только метки."""
    try:
        data = json.loads(_registry_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [item for item in items
            if isinstance(item, dict) and item.get("id")]


def prune_registry() -> dict:
    """Выкинуть призраков: запись без копии кук - не аккаунт.

    Бывает после ручных правок или сбоя: UI показывал аккаунт, которого
    нечем скачать, а «забыть» не работал. Записи без файла удаляются и из
    реестра, и из памяти; сама копия, разумеется, не создаётся.
    """
    items = load_registry()
    keep = [item for item in items
            if has_account_cookies(str(item["id"]))]
    if len(keep) == len(items):
        return {"removed": []}
    removed = [str(item["id"]) for item in items
               if item not in keep]
    _save_registry(keep)
    return {"removed": removed}


def _save_registry(items: list[dict]) -> None:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"accounts": items}, ensure_ascii=False,
                              indent=2), encoding="utf-8")
    os.replace(tmp, path)


def account_labels() -> dict:
    """{id: label} для подписи привязок."""
    return {item["id"]: str(item.get("label") or "") for item in load_registry()}


def _store_cookies(account_id: str, cookies) -> int:
    """Шифруем и кладём копию кук аккаунта (атомарно)."""
    text = export_netscape(cookies)
    target = account_cookies_path(account_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_bytes(encrypt(text.encode("utf-8")))
    os.replace(tmp, target)
    return len(cookies)


def create_account(cookies, *, label: str = "") -> dict:
    """Новый аккаунт из кук. Возвращает запись реестра."""
    picked = [c for c in cookies if is_google_cookie(c)]
    if not picked:
        raise RuntimeError("среди кук нет ни одного Google/YouTube - "
                           "похоже, окно не вошло в аккаунт")
    account_id = uuid.uuid4().hex[:8]
    count = _store_cookies(account_id, picked)
    from .util import now_iso
    record = {"id": account_id, "label": label or f"аккаунт ({count} кук)",
              "since": now_iso()}
    _save_registry(load_registry() + [record])
    return record


def create_account_from_text(text: str, *, label: str = "импорт cookies.txt") -> dict:
    return create_account(parse_netscape(text), label=label)


def remove_account(account_id: str) -> dict:
    """Удалить аккаунт: копию кук и запись реестра."""
    removed = False
    try:
        account_cookies_path(account_id).unlink()
        removed = True
    except FileNotFoundError:
        pass
    items = [item for item in load_registry() if item["id"] != account_id]
    _save_registry(items)
    return {"removed": removed}


def has_account_cookies(account_id: str) -> bool:
    return account_cookies_path(account_id).is_file()


def decrypted_cookies_text(account_id: str) -> str | None:
    """Расшифрованная копия (None - аккаунта нет). Текст - секрет."""
    path = account_cookies_path(account_id)
    if not path.is_file():
        return None
    return decrypt(path.read_bytes()).decode("utf-8")


def temporary_cookiefile(account_id: str) -> Path | None:
    """Расшифровать куки аккаунта во временный файл для yt-dlp.

    Путь обязан пройти через release_temp(): куки на диске живут ровно
    столько, сколько идёт загрузка/запрос.
    """
    text = decrypted_cookies_text(account_id)
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


def cookies_facts(cookies) -> dict:
    """Диагностика поимки без единого значения: только счётчики и домены."""
    google = [c for c in cookies if is_google_cookie(c)]
    return {
        "total": len(cookies),
        "google": len(google),
        "domains": sorted({(c.domain or "").lstrip(".") for c in google})[:8],
        "names": sorted({c.name for c in google})[:12],
    }


# Копии не создаём: yt-dlp читает куки браузера напрямую (DPAPI внутри).
BROWSER_CHOICES = ("firefox", "edge", "chrome", "brave")
# Свежие Chrome/Edge (v127+) шифруют куки App-Bound Encryption: расшифровать
# их сторонним инструментам нельзя - yt-dlp получает DPAPI-ошибку.
# https://github.com/yt-dlp/yt-dlp/issues/10927
BROKEN_BROWSERS = ("edge", "chrome", "brave")


def browser_cookie_option(settings: dict):
    """Опция yt-dlp cookiesfrombrowser или None (режим не задан/пусто)."""
    name = str(settings.get("browser_cookies") or "none").strip().lower()
    if name not in BROWSER_CHOICES:
        return None
    return (name,)


def browser_warning(settings: dict) -> str:
    """Честное предупреждение для браузеров с ABE (пусто - всё в порядке)."""
    name = str(settings.get("browser_cookies") or "none").strip().lower()
    if name in BROKEN_BROWSERS:
        return (f"{name.capitalize()} v127+ шифрует куки (App-Bound "
                "Encryption) - прочитать их напрямую невозможно. Рабочие "
                "пути: Firefox или импорт cookies.txt.")
    return ""


def migrate_legacy(settings) -> str | None:
    """Старый одиночный google_cookies.bin -> первый аккаунт реестра.

    G1 хранил копию одним файлом + метки в settings; при переходе на
    привязки файл перекидывается в accounts/, метки уходят в реестр.
    Возвращает id мигрированного аккаунта (None - мигрировать нечего).
    """
    from .paths import google_cookies_path
    legacy = google_cookies_path()
    if load_registry():
        return None            # реестр уже есть - легаси не трогаем
    if not legacy.is_file():
        return None
    try:
        cookies = parse_netscape(decrypt_legacy_text(legacy))
    except (OSError, RuntimeError):
        # Неразборчивая копия - удаляем, чтобы не висела мёртвым грузом.
        try:
            legacy.unlink()
        except OSError:
            pass
        return None
    label = str(settings.get("google_account_label") or "") or None
    account = create_account(cookies, label=label or "вход выполнен (куки)")
    from .util import now_iso
    if settings.get("google_account_since"):
        # Дату бережём из старых настроек - она честнее «сегодня».
        items = load_registry()
        for item in items:
            if item["id"] == account["id"]:
                item["since"] = str(settings["google_account_since"])
        _save_registry(items)
    try:
        legacy.unlink()
    except OSError:
        pass
    return account["id"]


def decrypt_legacy_text(path: Path) -> str:
    return decrypt(Path(path).read_bytes()).decode("utf-8")
