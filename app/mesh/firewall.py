"""Правило брандмауэра для mesh-порта: входящий TCP/UDP по согласию.

Включение участия = согласие и на это: без входящего правила сосед
(и всякий поиск по нику) не сможет открыть к вам TLS-канал - обрыв
необъясним, если о проблеме не сказать. Замер на живой паре: хост в
сети, порт слушается, а входящий SYN не проходит - классика правила,
которого нет для ЭТОГО пути exe (у «omnistash.exe» и «omnistash (1).exe»
разные правила, а отклонённое окно Windows остаётся отклонённым).

netsh/PowerShell для создания требуют прав администратора: не вышло -
мы НЕ повышаемся молча, а пишем в журнал готовую команду для
однократного ручного запуска. Проверка существования - через
Get-NetFirewallRule (вывод объектов, не зависит от локали ОС).
"""

from __future__ import annotations

import os
import subprocess
import sys

CREATE_NO_WINDOW = 0x08000000      # не мелькать консольным окном
PS_TIMEOUT = 20.0
RULE_PREFIX = "Omnistash mesh"


def _exe() -> str:
    """Путь к программе: в onefile-сборке sys.executable = сам exe."""
    return sys.executable


def _rule_name(port: int, proto: str) -> str:
    return f"{RULE_PREFIX} {port} {proto}"


def _powershell(script: str) -> str:
    """Запуск однозначного PowerShell-фрагмента; RuntimeError при отказе."""
    done = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, timeout=PS_TIMEOUT,
        creationflags=CREATE_NO_WINDOW)
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or b"").decode("utf-8", "replace")
        raise RuntimeError(detail.strip()[:300] or f"код {done.returncode}")
    return done.stdout.decode("utf-8", "replace").strip()


def manual_command(port: int) -> str:
    """Готовая команда для запуска от имени администратора (для журнала)."""
    exe = _exe().replace("'", "''")
    parts = []
    for proto in ("TCP", "UDP"):
        name = _rule_name(port, proto).replace("'", "''")
        parts.append(
            f"New-NetFirewallRule -DisplayName '{name}' -Direction Inbound "
            f"-Action Allow -Protocol {proto} -LocalPort {port} "
            f"-Program '{exe}' -Profile Private | Out-Null")
    return ("powershell -Command \""
            + "; ".join(parts) + "\"")


# Зона перехвата стандартных DPI-утилит (zapret и родня): их --wf-списки
# - «80,443,2053,2083,2087,2096,8443,1024-65535». Порт ВНЕ зоны (например
# 1000 или 444-1023) такой программой физически не перехватывается -
# это главный обход «работаем параллельно, ничего не выключая».
_DPI_ZONE_HIGH = frozenset((80, 443, 2053, 2083, 2087, 2096, 8443))


def in_dpi_zone(port: int) -> bool:
    """Попадает ли порт под типовой перехват DPI-утилит?"""
    port = int(port)
    return port >= 1024 or port in _DPI_ZONE_HIGH


def dpi_running() -> bool:
    """Обнаружен ли DPI-перехватчик (zapret/winws и подобные) - Windows."""
    if os.name != "nt":
        return False
    script = ("if (Get-Process -Name winws,winwsx,wfp -ErrorAction "
              "SilentlyContinue) { 'yes' } elseif (Get-Service -Name zapret,"
              " winws -ErrorAction SilentlyContinue | Where-Object "
              "{$_.Status -eq 'Running'}) { 'yes' } else { 'no' }")
    try:
        return "yes" in _powershell(script).lower()
    except RuntimeError:
        return False


def ensure_rules(port: int, log) -> bool:
    """Правила входящего на месте? True - да (создали или уже были).

    Один вызов на протокол: PowerShell сам решает «есть/создать» -
    вывод объектов не зависит от локали, дубликаты не плодятся.
    """
    if os.name != "nt":
        return True                     # не Windows - правила нечего создавать
    exe = _exe()
    ok_all = True
    for proto in ("TCP", "UDP"):
        name = _rule_name(port, proto).replace("'", "''")
        script = (
            "if (Get-NetFirewallRule -DisplayName '" + name + "' "
            "-ErrorAction SilentlyContinue) { 'exists' } else { "
            "New-NetFirewallRule -DisplayName '" + name + "' "
            "-Direction Inbound -Action Allow -Protocol " + proto +
            " -LocalPort " + str(int(port)) + " -Program '" +
            exe.replace("'", "''") + "' -Profile Private | Out-Null; "
            "'created' }")
        try:
            result = _powershell(script)
        except RuntimeError as exc:
            log(f"Брандмауэр: правило {proto}/{port} создать не удалось "
                f"({exc}). Без него к вам не подключатся - запустите "
                f"один раз от администратора: {manual_command(port)}")
            ok_all = False
            continue
        if result.endswith("created"):
            log(f"Брандмауэр: разрешено входящее {proto}/{port} "
                f"для {_exe()}")
    return ok_all
