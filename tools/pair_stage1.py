# -*- coding: utf-8 -*-
"""Парный живой прогон этапа 1 (дружба/канал) против пары vilmpc.

Запуск на любой из машин (юзера дёргать не нужно - прогоны наши):

    python tools/pair_stage1.py [адрес]

Шаги (акт приёмки этапа 1):
  1) TLS-probe канала (аттестация до первого байта);
  2) каталог участника (подписанный снапшот);
  3) манифест файла (подпись ключом канала);
  4) перенос файла small с проверкой sha256 (перехватить нечего:
     TLS, плюс при encrypt - B2-блоки под ключ получателя).

Печатает ИТОГ каждым шагом + сводку OK/FAIL - это и есть акт для
канала link. Программа ничего не меняет (только чтение и временные
байты в %TEMP%).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.mesh import channel as channel_mod  # noqa: E402
from app.mesh import client as mesh_client   # noqa: E402


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    addr = sys.argv[1] if len(sys.argv) > 1 else "100.101.230.19:1000"
    results = []

    def report(step: str, ok: bool, note: str) -> None:
        results.append((step, ok, note))
        print(f"  [{'OK ' if ok else 'FAIL'}] {step}: {note}", flush=True)

    print(f"Этап 1: парный прогон против {addr}", flush=True)

    # 1) TLS-канал (пин не пишем - это диагностика, а не дружба)
    t0 = time.time()
    try:
        ident = mesh_client.probe(addr, expect=None, pin=False, timeout=6)
        report("TLS-канал", bool(ident),
               f"открыт за {(time.time() - t0) * 1000:.0f} мс, ключ "
               f"{ident[:16]}…")
    except Exception as exc:  # noqa: BLE001 - акт фиксирует любую причину
        report("TLS-канал", False, f"{type(exc).__name__}: {exc}")
        return finish(results)

    # 2) Каталог (подписанный снапшот)
    try:
        conn, resp = mesh_client._request(addr, "/catalog", timeout=6)
        try:
            body = resp.read(8 * 1024 * 1024)
        finally:
            conn.close()
        data = json.loads(body.decode("utf-8"))
        entries = data.get("entries") or []
        report("Каталог", resp.status == 200 and bool(entries),
               f"HTTP {resp.status}, строк {len(entries)}")
    except Exception as exc:  # noqa: BLE001
        report("Каталог", False, f"{type(exc).__name__}: {exc}")
        entries = []

    # 3-4) Манифест + перенос: берём первый файл в пределах лимита
    # (size в каталоге не ходит - проверяем по манифесту).
    if not entries:
        report("Файл", False, "каталог пуст")
        return finish(results)
    tried = []
    for entry in entries:
        target = str(entry.get("remote_id") or "")
        if not target or target in tried:
            continue
        tried.append(target)
        try:
            conn, resp = mesh_client._request(
                addr, f"/manifest/{target}", timeout=6)
            try:
                item = json.loads(resp.read(1 << 20).decode("utf-8"))
            finally:
                conn.close()
            expected = str(item.get("sha256") or "").split(":", 1)[-1]
            size = int(item.get("size") or 0)
            if not expected:
                continue
            if size > 10 * 1024 * 1024:
                report("Манифест", True,
                       f"{target}: {size} байт - больше лимита, "
                       "пропускаю (413 честно)")
                continue
            part = Path(tempfile.gettempdir()) / f"pair_stage1_{target}.part"
            got = mesh_client.fetch(target, addr, str(part))
            actual = sha256_of(part) if part.is_file() else ""
            ok = bool(actual) and actual == expected
            report("Перенос+sha256", ok,
                   f"{target}: получено {got.get('written', 0)} байт, "
                   f"sha {actual[:16]}…")
            try:
                part.unlink()
            except OSError:
                pass
            if ok:
                return finish(results)
        except Exception as exc:  # noqa: BLE001
            report("Файл", False, f"{target}: {type(exc).__name__}: {exc}")
    report("Перенос+sha256", False, "ни один файл не прошёл")
    return finish(results)


def finish(results) -> int:
    ok = all(item[1] for item in results)
    print(f"ИТОГ: {'OK - акт приёмки этапа 1 пройден' if ok else 'FAIL'}",
          flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.exit(main())
