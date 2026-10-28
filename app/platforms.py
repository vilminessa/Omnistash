"""Реестр площадок (P1): одна запись = одна поддерживаемая площадка.

Сквозная система вместо зашитых констант: классификатор ссылок,
взрослые флаги (гейт18+), подписи вкладок и канонические URL берутся
отсюда. Новая площадка = новая запись + её правила классификации
(см. metadata.classify_url); юр-флаги (adult/аккаунт/AUP) включают
готовые механизмы диалогов, а не пишутся заново в коде.

Поля записи:
  id       - ключ платформы в БД (videos.platform и др.);
  label    - подпись для окна (вкладка, список источников);
  domains  - хосты площадки (конечное сравнение, с учётом поддоменов);
  adult    - возрастной контент: свой гейт 18+, скрыт в общих вкладках;
  account  - площадка живёт с куки/входом аккаунта (подсказка UI);
  token_capability - что площадка принимает вместо «подтвердите, что вы не
             бот»: "po" (visitor-info/PO-токен), "age" (подтверждение
             возраста cookie-площадкой), None - не принимает (тогда отказ
             «bot» лечится только аккаунтом/куками и mesh).
"""

from __future__ import annotations

PLATFORMS: tuple = (
    {
        "id": "youtube",
        "label": "YouTube",
        "domains": ("youtube.com", "youtu.be", "youtube-nocookie.com"),
        "adult": False,
        "account": True,
        # Анти-бот YouTube принимает visitor-info и PO-токен (M13).
        "token_capability": "po",
    },
    {
        "id": "pornhub",
        "label": "PornHub",
        # зеркала, которые yt-dlp знает тоже: pornhub.(com|net|org)
        # с любым поддоменом (rt./www./мобильные)
        "domains": ("pornhub.com", "pornhub.org", "pornhub.net"),
        "adult": True,
        "account": False,
        # Площадка требует подтверждение возраста (cookie-подтверждение),
        # токена содержимого у неё нет.
        "token_capability": "age",
    },
    {
        "id": "vk",
        "label": "VK Видео",
        "domains": ("vk.com", "vkvideo.ru", "mail.ru", "my.mail.ru"),
        "adult": False,
        "account": True,     # часть клипов требует входа (куки аккаунта)
        "token_capability": None,
    },
)

_BY_ID = {item["id"]: item for item in PLATFORMS}


def by_id(platform_id) -> dict | None:
    """Запись площадки по идентификатору (или None)."""
    return _BY_ID.get(str(platform_id or ""))


def by_host(host: str) -> dict | None:
    """Запись площадки по хосту ссылки (с конечным сравнением домена)."""
    host = str(host or "").lower().split(":")[0]
    if not host:
        return None
    for item in PLATFORMS:
        for domain in item["domains"]:
            if host == domain or host.endswith("." + domain):
                return item
    return None


def is_adult(platform_id) -> bool:
    """Возрастная ли площадка (скрытие в общих вкладках, гейт18+)."""
    item = by_id(platform_id)
    return bool(item and item.get("adult"))


def adult_ids() -> tuple:
    """Идентификаторы взрослых площадок (фильтры общих списков)."""
    return tuple(item["id"] for item in PLATFORMS if item.get("adult"))


def token_capability(platform_id) -> str | None:
    """Что площадка принимает вместо «подтвердите, что вы не бот» (M13):
    "po", "age" или None. Нейтральная механика, имя площадки не меняется."""
    item = by_id(platform_id)
    value = item.get("token_capability") if item else None
    return str(value) if value else None
