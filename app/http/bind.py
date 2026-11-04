"""Привязка HTTP-слушателя клиента (docs/SERVER-CLIENT.md §7, шаг S6).

Четыре режима `serve_bind`:

  loopback  - 127.0.0.1, только этот компьютер; наружу ничего не уходит;
  lan       - адрес сетевого интерфейса этого компьютера; требует явного
              согласия человека («что уходит наружу»);
  tailscale - адрес из диапазона Tailscale (100.64.0.0/10) - то же согласие;
  wildcard  - 0.0.0.0 («обычный порт»): требует согласия И allowlist - без
              списка адресов слушатель открылся бы всему, кто достает порт.

Отказ решается здесь и называется причиной: «согласия нет», «адрес
интерфейса не найден», «allowlist пуст или битый». Тихо включить доступ
наружу нельзя, и «не получилось» не превращается в «включили».

Сервер также проверяет адрес клиента по allowlist (bind.allowed): право на
соединение - не «кто-то подключился», а адрес из списка, который человек
назвал.
"""

from __future__ import annotations

import ipaddress
import os
import re
import subprocess
import time
from typing import Callable

LOOPBACK = "127.0.0.1"
WILDCARD = "0.0.0.0"
MODES = ("loopback", "lan", "tailscale", "wildcard")

# Tailscale раздаёт адреса из CGNAT-диапазона (100.64.0.0/10).
CGNAT = ipaddress.ip_network("100.64.0.0/10")
# Автоматически назначенный адрес без DHCP - не «сеть этого компьютера».
APIPA = ipaddress.ip_network("169.254.0.0/16")

CREATE_NO_WINDOW = 0x08000000
PS_TIMEOUT = 12.0
_SPLIT = re.compile(r"[,\s;]+")
# Кэш адресов машины: подпроцесс PowerShell стоит ~1.2 с, а интерфейсы
# меняются редко. TTL маленький: человек, который только что поднял VPN или
# Tailscale, не должен ждать.
ADDRESSES_TTL = 5.0
_ADDRESSES_CACHE: list[str] | None = None
_ADDRESSES_STAMP = 0.0


def normalize_mode(value) -> str:
    """Значение настройки -> режим; чужое слово читается как loopback (§7)."""
    text = str(value or "loopback").strip().lower()
    aliases = {"localhost": "loopback", "127.0.0.1": "loopback",
               "this": "loopback", "computer": "loopback",
               "lan": "lan", "local-network": "lan", "network": "lan",
               "tailscale": "tailscale", "ts": "tailscale",
               "zero": "wildcard", "all": "wildcard", "0.0.0.0": "wildcard",
               "wildcard": "wildcard"}
    return aliases.get(text, "loopback")


def read_addresses() -> list[str]:
    """IPv4-адреса машины. Windows - Get-NetIPAddress (вывод объектов не
    зависит от локали); вне Windows список пуст: серверный режим Windows
    (docs/SERVER-CLIENT.md §1), чужую ОС наружу не открываем.

    Список кэшируется на ADDRESSES_TTL секунд: интерфейсы меняются редко,
    а подпроцесс стоит ~1.2 с - читать адреса на каждый вызов резать было
    бы медленной работой без нужды. Кэш не меняет решений: «адрес не найден»
    остаётся тем же фактом, что и без кэша. Сброс - forget_addresses()
    (смена интерфейсов и тесты).
    """
    global _ADDRESSES_CACHE, _ADDRESSES_STAMP
    if os.name != "nt":
        return []
    now = time.monotonic()
    if _ADDRESSES_CACHE is not None and now - _ADDRESSES_STAMP < ADDRESSES_TTL:
        return list(_ADDRESSES_CACHE)
    script = ("Get-NetIPAddress -AddressFamily IPv4 -ErrorAction "
              "SilentlyContinue | Select-Object -ExpandProperty IPAddress")
    try:
        done = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=PS_TIMEOUT,
            creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return []
    if done.returncode != 0:
        return []
    out = done.stdout.decode("utf-8", "replace")
    found = [line.strip() for line in out.splitlines() if line.strip()]
    _ADDRESSES_CACHE = found
    _ADDRESSES_STAMP = time.monotonic()
    return list(found)


def forget_addresses() -> None:
    """Сбросить кэш адресов: интерфейсы машины поменялись или тесты.

    Пустой список без чтения означал бы «не смотрели», а не «адресов нет»;
    поэтому сброс называет причину и следующий читатель идёт к системе.
    """
    global _ADDRESSES_CACHE, _ADDRESSES_STAMP
    _ADDRESSES_CACHE = None
    _ADDRESSES_STAMP = 0.0


def parse_allowlist(text) -> list:
    """Список адресов/CIDR через запятую или пробел.

    Битый элемент - ValueError с именем: «половина списка понята» было бы
    доступом, которого человек не планировал.
    """
    nets: list = []
    for part in _SPLIT.split(str(text or "").strip()):
        if not part:
            continue
        try:
            if "/" in part:
                nets.append(ipaddress.ip_network(part, strict=True))
            else:
                nets.append(ipaddress.ip_network(part + "/32", strict=True))
        except ValueError as exc:
            raise ValueError(f"неверный адрес в allowlist: {part} ({exc})") from exc
    return nets


def allowed(addr: str, nets: list) -> bool:
    """Адрес клиента в списке? Loopback всегда свой (слушатель на этом
    компьютере не «чужой»), остальные - только по списку."""
    try:
        ip = ipaddress.ip_address(str(addr))
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    if not nets:
        return False
    return any(ip in net for net in nets)


def _lan_address(addresses: list) -> str:
    for value in addresses:
        try:
            ip = ipaddress.ip_address(value)
        except ValueError:
            continue
        if ip.is_loopback or ip in CGNAT or ip in APIPA:
            continue
        return value
    return ""


def _tailscale_address(addresses: list) -> str:
    for value in addresses:
        try:
            ip = ipaddress.ip_address(value)
        except ValueError:
            continue
        if ip in CGNAT:
            return value
    return ""


def resolve(mode, consent, allowlist, addresses: Callable[[], list] | None = None) -> dict:
    """Режим настройки -> адрес привязки с честным отказом.

    Возвращает {mode, address, error, note}: error - «наружу не выходим»,
    note - что человек должен знать про открытую сеть. Address при отказе
    loopback: отказ не должен оставить слушатель на чужом адресе.
    """
    mode = normalize_mode(mode)
    # Адреса машины читаются только там, где режим их требует: loopback и
    # 0.0.0.0 их не используют (wildcard привязывается на все интерфейсы и
    # решает список адресов), поэтому резать их ради «наружу ничего не
    # уходит» было бы медленным молчанием - подпроцесс ~1.2 с на вызов.
    def read() -> list:
        return list(addresses()) if addresses else read_addresses()

    if mode == "loopback":
        return {"mode": "loopback", "address": LOOPBACK, "error": "",
                "note": "только этот компьютер: наружу ничего не уходит"}
    if not consent:
        return {"mode": mode, "address": LOOPBACK,
                "error": "согласия «что уходит наружу» нет - слушатель "
                         "остаётся на этом компьютере",
                "note": ""}
    try:
        nets = parse_allowlist(allowlist)
    except ValueError as exc:
        return {"mode": mode, "address": LOOPBACK, "error": str(exc), "note": ""}
    if mode == "lan":
        address = _lan_address(read())
        if not address:
            return {"mode": mode, "address": LOOPBACK,
                    "error": "адрес сети этого компьютера не найден - "
                             "LAN-интерфейс не поднялся", "note": ""}
        return {"mode": mode, "address": address, "error": "",
                "note": f"сеть открыта для тех, кто достает порт ({address}): "
                        f"доступ решают скоупы на ключе, а не адрес"}
    if mode == "tailscale":
        address = _tailscale_address(read())
        if not address:
            return {"mode": mode, "address": LOOPBACK,
                    "error": "адрес Tailscale не найден (диапазон "
                             "100.64.0.0/10) - интерфейс не поднялся",
                    "note": ""}
        return {"mode": mode, "address": address, "error": "",
                "note": f"доступ через Tailscale ({address}): наружу уходит "
                        "тот же трафик, что через LAN"}
    # wildcard: «обычный порт» - без списка адресов открылся бы всем.
    if not nets:
        return {"mode": mode, "address": LOOPBACK,
                "error": "обычный порт (0.0.0.0) требует allowlist: без "
                         "списка адресов слушатель открылся бы всему, кто "
                         "достает порт", "note": ""}
    return {"mode": mode, "address": WILDCARD, "error": "",
            "note": f"порт открыт на всех интерфейсах, принимаются адреса из "
                    f"allowlist: {len(nets)} сетей"}
