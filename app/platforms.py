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
  account  - площадка живёт с куки/входом аккаунта (подсказка UI).
"""

from __future__ import annotations

PLATFORMS: tuple = (
    {
        "id": "youtube",
        "label": "YouTube",
        "domains": ("youtube.com", "youtu.be", "youtube-nocookie.com"),
        "adult": False,
        "account": True,
    },
    {
        "id": "pornhub",
        "label": "PornHub",
        "domains": ("pornhub.com",),
        "adult": True,
        "account": False,
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
