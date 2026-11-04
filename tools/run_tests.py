"""Параллельный прогон офлайн-тестов (ускорение проверки перед коммитом и в CI).

unittest discover гоняет модули последовательно: на этой машине полный набор
идёт ~19 минут, и почти всё время - не проверок, а работа SetUp модуля (временный
профиль, схема SQLite, импорт yt-dlp, слушатель TLS). Модули независимы:
каждый свой профиль и база во временной папке, никакого общего состояния,
поэтому их можно гонять в отдельных процессах и это не меняет проверок.

    python tools/run_tests.py              # все модули, параллельно по ядрам
    python tools/run_tests.py --jobs 1     # последовательно (для отладки)
    python tools/run_tests.py --only test_i18n test_serve   # выборка
    python tools/run_tests.py --list       # что будет запущено, без прогона

Итог печатается одной строкой в форме unittest (Ran N tests ...), плюс хвост
отказа каждого модуля - молчания нет: упавший модуль называется своим именем,
числом проверок и временем. Живые тесты (OMNISTASH_LIVE=1) в этом прогоне не
запускаются: они остаются тем же skip, что и в discover.
"""

from __future__ import annotations

import concurrent.futures
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
SUMMARY = re.compile(r"Ran (\d+) tests? in [\d.]+s")
SKIPPED = re.compile(r"skipped=(\d+)")
FAILED = re.compile(r"failures=(\d+)")
ERRORS = re.compile(r"errors=(\d+)")
DEFAULT_TIMEOUT = 420.0


def modules(only: list[str]) -> list[str]:
    """Имена тестовых модулей (без расширения), сортировка стабильна."""
    found = sorted(path.stem for path in TESTS.glob("test_*.py") if path.is_file())
    if not only:
        return found
    wanted = [name if name.startswith("test_") else f"test_{name}" for name in only]
    missing = [name for name in wanted if name not in found]
    if missing:
        raise ValueError(f"модулей тестов не найдено: {missing} "
                         f"(каталог {TESTS}) - выборка не прогоняется молча")
    return wanted


def jobs(requested: int | None) -> int:
    cores = os.cpu_count() or 1
    if requested is None:
        # 12 ядер на этой машине, 4 на CI-раннере: больше ядер, чем модулей,
        # смысла не имеет - лишние процессы только жрут импорт yt-dlp
        return max(1, min(cores, 10))
    if requested < 1:
        raise ValueError(f"--jobs {requested} не право: нужен процесс от 1")
    return requested


def run_module(name: str, timeout: float) -> dict:
    started = time.perf_counter()
    try:
        proc = subprocess.run([sys.executable, "-m", "unittest", f"tests.{name}"],
                              cwd=str(ROOT), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
        code, out = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        code, out, timed_out = 1, partial + "\n" + f"модуль не закончился за {timeout} с", True
    elapsed = time.perf_counter() - started
    ran = SUMMARY.search(out)
    skipped = SKIPPED.search(out)
    failures = FAILED.search(out)
    errors = ERRORS.search(out)
    return {
        "module": name,
        "code": code,
        "timed_out": timed_out,
        "ran": int(ran.group(1)) if ran else 0,
        "skipped": int(skipped.group(1)) if skipped else 0,
        "failures": int(failures.group(1)) if failures else 0,
        "errors": int(errors.group(1)) if errors else 0,
        "seconds": round(elapsed, 1),
        "tail": out.strip().splitlines()[-14:],
    }


def report(results: list[dict], wall: float) -> tuple[int, list[str]]:
    results = sorted(results, key=lambda item: item["module"])
    slow = sorted(results, key=lambda item: -item["seconds"])[:8]
    lines = []
    for item in slow:
        lines.append(f"самые долгие: {item['module']} - {item['seconds']} с "
                     f"({item['ran']} проверок)")
    ran = sum(item["ran"] for item in results)
    skipped = sum(item["skipped"] for item in results)
    failures = sum(item["failures"] for item in results)
    errors = sum(item["errors"] for item in results)
    bad = [item for item in results if item["code"] != 0]
    for item in bad:
        lines.append(f"ОШИБКА: модуль {item['module']} ({item['ran']} проверок, "
                     f"{item['seconds']} с)"
                     + (" - не закончился за отведённое время" if item["timed_out"] else ""))
        lines += [f"    {line}" for line in item["tail"]]
    if not bad:
        summary = f"Ran {ran} tests in {round(wall, 1)}s"
        if skipped:
            summary += f" - OK (skipped={skipped})"
        else:
            summary += " - OK"
        lines.append(summary)
        lines.append(f"модулей: {len(results)}, проверок: {ran}, "
                     f"стена: {round(wall, 1)} с (последовательный discover дал бы "
                     f"сумму {round(sum(item['seconds'] for item in results), 1)} с)")
    else:
        lines.append(f"ИТОГ: проверок {ran}, отказов модулей {len(bad)}, "
                     f"failures={failures}, errors={errors} - молчания нет")
    return (1 if bad else 0), lines


def main(argv: list[str]) -> int:
    only: list[str] = []
    requested: int | None = None
    timeout = DEFAULT_TIMEOUT
    list_only = False
    index = 0
    while index < len(argv):
        flag = argv[index]
        if flag == "--jobs":
            index += 1
            if index >= len(argv):
                print("отказ: --jobs без числа", file=sys.stderr)
                return 1
            requested = int(argv[index])
        elif flag == "--only":
            index += 1
            while index < len(argv) and not argv[index].startswith("--"):
                only.append(argv[index])
                index += 1
            continue
        elif flag == "--timeout":
            index += 1
            timeout = float(argv[index])
        elif flag == "--list":
            list_only = True
        elif flag in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            print(f"отказ: неизвестный флаг {flag!r}", file=sys.stderr)
            return 1
        index += 1

    try:
        wanted = modules(only)
        workers = jobs(requested)
    except ValueError as exc:
        print(f"отказ: {exc}", file=sys.stderr)
        return 1

    if list_only:
        print(f"модулей: {len(wanted)}, процессов: {workers}")
        for name in wanted:
            print(f"  {name}")
        return 0

    started = time.perf_counter()
    results: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run_module, name, timeout) for name in wanted]
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
    code, lines = report(results, time.perf_counter() - started)
    for line in lines:
        print(line, file=sys.stderr if code else sys.stdout)
    if code:
        print("набор не прошёл: причины названы выше", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
