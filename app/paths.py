"""Пути Omnistash: код, профиль, рабочие папки.

Два корня, которые не стоит путать:
  base_dir()   - где лежит код (или exe при заморозке): сюда кладётся то,
                 что рядом с программой (bin/ffmpeg.exe и подобное);
  profile_dir() - постоянный профиль в %LOCALAPPDATA%\\Omnistash: настройки,
                 база библиотеки, журналы. Переживает обновления exe.
Исключения одно: ui_src/ в собранной exe живёт в бандле (см. ui_dir).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "Omnistash"


def base_dir() -> Path:
    """Папка программы: рядом с exe в заморозке, с omnistash.py в исходниках."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def ui_dir() -> Path:
    """Исходники интерфейса (html/css/js), вшиваются в страницу окна.

    В собранной exe они лежат в бандле (_MEIPASS, куда PyInstaller вытаскивает
    данные), а не в папке программы: там у человека живут bin/ и прочее
    «рядом с программой». Поэтому сначала ищем в бандле, и только потом -
    как в исходниках, рядом с кодом.
    """
    bundled = Path(getattr(sys, "_MEIPASS", "")) / "ui_src"
    if bundled.is_dir():
        return bundled
    return base_dir() / "ui_src"


def profile_dir() -> Path:
    """%LOCALAPPDATA%\\Omnistash - создаётся при первом обращении."""
    root = Path(os.environ.get("LOCALAPPDATA", str(base_dir()))) / APP_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def settings_path() -> Path:
    """Файл настроек профиля."""
    return profile_dir() / "settings.json"


def legal_path() -> Path:
    """Файл юридических согласий (датированное «Принять», LEG-1/LEG-3)."""
    return profile_dir() / "legal.json"


def db_path() -> Path:
    """Файл базы библиотеки (индекс скачанного)."""
    return profile_dir() / "library.db"


def log_path() -> Path:
    """Журнал приложения (плюс stdout в консоль при отладке)."""
    return profile_dir() / "omnistash.log"


def google_cookies_path() -> Path:
    """Зашифрованная (DPAPI) копия кук аккаунта Google.

    Сам файл - не секрет (без ключа пользователя он мусор), но и его
    держим в профиле: «Забыть аккаунт» просто удаляет файлы.
    """
    return profile_dir() / "google_cookies.bin"


def google_tokens_path() -> Path:
    """Зашифрованный (DPAPI) refresh-токен OAuth (G1b)."""
    return profile_dir() / "google_tokens.bin"


def bypass_cache_path() -> Path:
    """Кэш выигравшей «дороги выкачки» по площадке (M13).

    Не секрет: имена дорог и момент проверки. Тот же приём, что кэш стратегии
    в Synfronia (dpi.py:1155-1205) - отдельный файл рядом с профилем.
    """
    return profile_dir() / "bypass_routes.json"


def platform_tokens_path() -> Path:
    """Зашифрованные (DPAPI) подтверждения «я человек» площадок (M13).

    visitor-info / PO-токен / подтверждающие cookie площадки - секретные
    значения сессии, поэтому шифруются тем же способом, что куки аккаунта
    (google_auth.encrypt): без учётки Windows файл бесполезен.
    """
    return profile_dir() / "platform_tokens.bin"


def serve_tokens_path() -> Path:
    """Зашифрованный (DPAPI) реестр токенов доступа HTTP-клиента (SV-C, S1).

    Скоупы ключа - право читать/качать/менять: plaintext на диске означал бы
    доступ для любого, кто получил файл профиля.
    """
    return profile_dir() / "serve_tokens.bin"


def serve_audit_path() -> Path:
    """Журнал обращений HTTP-сервера: ключ -> метод -> код -> адрес (S1).

    Не секрет: метки токенов и коды ответов, без значений токенов и путей.
    """
    return profile_dir() / "serve_audit.log"
