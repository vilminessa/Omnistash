r"""Capped-runner: жёсткий потолок commit-памяти для локальных мерок.

    python tools/run_capped.py --cmd "flutter test -j 1" --max-mem-mb 2048
    python tools/run_capped.py --cmd "flutter build windows --release" --max-mem-mb 4096

Назначение: flutter_tester резервирует арены heaps по гигабайтам (факт
прогона: осиротевший процесс держал 30.8 ГБ commit и забрал память машины
человека), а dart-фронт при компиляции тестов добавляет свою. Рантайм
окна ничего не скачивает и ничего не измеряет - это тулчейн сборочного
времени, лимит здесь страховка машины, не мера корректности.

История формы: первая версия была Windows Job (tools/run_capped.ps1,
MAX_MEMORY + KILL_ON_JOB_CLOSE) и отказала на CreateJobW: вложенный Job
в песочнице запуска не создаётся (факт smoke). Второй вариант читал
дерево процессов через CreateToolhelp32Snapshot - Process32FirstW
упорно возвращал FALSE с ERROR_MORE_DATA(24) в той же песочнице (факт
прогона dbg), поэтому перечисление взято у WMI (Get-CimInstance
Win32_Process: ProcessId, ParentProcessId, Name, PrivatePageCount),
который в песочнице работает. Воркер - один живый powershell на весь
прогон: запрос по строке stdin, ответ до маркера END; процесс не
спавнится каждый тик (не грузит диск).

Лимит применяется как наблюдение: суммарный commit потомков корня
сверяется каждые interval; превышение ceiling убивает дерево через
taskkill /T /F (коды возврата 137), пик печатается фактом. После выхода
убиваются осиротевшие процессы SDK-имён (flutter_tester, dartvm,
dartaotruntime), чей родитель уже мёртв - именно они оставляют гигабайты
commit. Принудительный сброс resident (working-set trim) не включён: он
гоняет подкачку и грузит диск (сообщение человека).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time

# Имена SDK-инструментов Flutter: осиротевшие процессы с этими именами
# убиваются всегда (на этой машине их порождает только flutter-тулчейн).
ORPHAN_NAMES = {"flutter.exe", "dart.exe", "dartaotruntime.exe",
                "dartvm.exe", "flutter_tester.exe", "flutter_test",
                "dart"}

WORKER_SCRIPT = (
    "while ($true) {"
    "  $line = [Console]::In.ReadLine();"
    "  if ($null -eq $line -or $line -eq 'quit') { break };"
    "  try {"
    "    $ps = Get-CimInstance -Query "
    "'SELECT ProcessId,ParentProcessId,Name,PrivatePageCount FROM Win32_Process';"
    "  } catch { continue };"
    "  $out = foreach ($p in $ps) {"
    "    ('{0}|{1}|{2}|{3}' -f $p.ProcessId, $p.ParentProcessId,"
    "     $p.Name, $p.PrivatePageCount)"
    "  };"
    "  [Console]::Out.Write(($out -join \"`n\") + \"`nEND`n\")"
    "}"
)


def read_snapshot(worker: subprocess.Popen) -> dict[int, tuple[int, int, str]]:
    """Один снимок WMI: pid -> (parent_pid, commit_bytes, имя)."""
    worker.stdin.write("tick\n")
    worker.stdin.flush()
    procs: dict[int, tuple[int, int, str]] = {}
    while True:
        line = worker.stdout.readline()
        if not line or line.strip() == "END":
            break
        parts = line.strip().split("|")
        if len(parts) != 4:
            continue
        pid, parent, name, commit = parts
        procs[int(pid)] = (int(parent), int(commit or 0), name)
    return procs


def tree_sum(procs: dict[int, tuple[int, int, str]], root: int) -> tuple[list[int], int]:
    """Потомки корня (дерево по parent_pid) и сумма их commit."""
    children: dict[int, list[int]] = {}
    for pid, (parent, _commit, _name) in procs.items():
        children.setdefault(parent, []).append(pid)
    found: list[int] = []
    stack = [root]
    while stack:
        pid = stack.pop()
        if pid in found:
            continue
        found.append(pid)
        stack.extend(children.get(pid, []))
    total = sum(procs[pid][1] for pid in found if pid in procs)
    return found, total


def orphans(procs: dict[int, tuple[int, int, str]]) -> list[int]:
    """Живые SDK-процессы с мёртвым родителем - осиротевшие арены commit."""
    out = []
    for pid, (parent, _commit, name) in procs.items():
        if name.lower() in ORPHAN_NAMES and (parent not in procs or parent == 0):
            out.append(pid)
    return out


def kill_tree(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def kill_pid(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="мерка под потолком commit-памяти")
    parser.add_argument("--cmd", required=True, help="строка для cmd /c")
    parser.add_argument("--max-mem-mb", type=int, default=2048)
    parser.add_argument("--interval-ms", type=int, default=800)
    parser.add_argument("--workdir", default=None)
    parser.add_argument("--top-n", type=int, default=0,
                        help="печатать пики commit по именам процессов "
                             "(0 - не печатать): кто именно держит гигабайты")
    args = parser.parse_args(argv)

    worker = subprocess.Popen(
        ["powershell", "-NoProfile", "-Command", WORKER_SCRIPT],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")

    proc = subprocess.Popen(args.cmd, shell=True, cwd=args.workdir)
    limit_bytes = args.max_mem_mb * 1024 * 1024
    peak = 0
    name_peaks: dict[str, int] = {}
    capped = False
    code = 0
    try:
        while proc.poll() is None:
            try:
                procs = read_snapshot(worker)
            except (OSError, UnicodeError) as failure:
                # Воркер умер - без наблюдения потолок не держится, поэтому
                # дерево гасится, а не остаётся бесконтрольным.
                print(f"воркер снимка не работает ({failure}) - дерево убивается",
                      file=sys.stderr)
                kill_tree(proc.pid)
                capped = True
                break
            pids, used = tree_sum(procs, proc.pid)
            peak = max(peak, used)
            if args.top_n > 0:
                for pid in pids:
                    parent, commit, name = procs.get(pid, (0, 0, "?"))
                    name_peaks[name] = max(name_peaks.get(name, 0), commit)
            if used > limit_bytes:
                capped = True
                print(f"потолок {args.max_mem_mb} MB превышен: commit "
                      f"{used / (1024 * 1024):.0f} MB - дерево {len(pids)} "
                      "процессов убивается", file=sys.stderr)
                kill_tree(proc.pid)
                break
            time.sleep(args.interval_ms / 1000.0)
        code = 137 if capped else proc.wait()
    finally:
        try:
            procs = read_snapshot(worker)
            stray = orphans(procs)
            if stray:
                print(f"осиротевшие SDK-процессы после прогона: {stray} - убиты",
                      file=sys.stderr)
                for pid in stray:
                    kill_pid(pid)
        except (OSError, ValueError):
            pass
        worker.stdin.write("quit\n")
        worker.stdin.flush()
        worker.wait(timeout=5)
        if args.top_n > 0 and name_peaks:
            top = sorted(name_peaks.items(), key=lambda kv: kv[1], reverse=True)[:args.top_n]
            print("пики commit по именам: " + ", ".join(
                f"{name}={mb / (1024 * 1024):.0f}MB" for name, mb in top))
        print(f"capped run: peak_mb={peak / (1024 * 1024):.0f}, "
              f"limit_mb={args.max_mem_mb}, код {code}")
    return 137 if capped else code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
