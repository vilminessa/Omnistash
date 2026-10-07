"""Линт .github/workflows - без зависимостей (только stdlib).

Что проверяем и почему:
  * невидимые символы (ZWSP/BOM/NBSP) - их невозможно увидеть при правке,
    но YAML с ними GitHub принимает, а шаг молча не то исполняет;
  * каждая внешняя ссылка `uses:` закреплена SHA-коммитом (или явно
    разрешённым тегом генератора SLSA): тег можно переписать - supply chain;
  * у каждого воркфлоу есть permissions на верхнем уровне (читает всё,
    пишет - только джоб, которому нужно);
  * `on` содержит push/pull_request или теги - воркфлоу, который нигде не
    запускается, врёт о наличии конвейера.

Запуск: python tools/check_workflows.py (в CI - шаг «Линт workflows»).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Раннер Windows отдаёт cp1252, а сообщения проекта русские: без этой
# строчки сам линт упадёт на собственном print() - как впервые и вышло.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        pass

INVISIBLE = "\ufeff\u200b\u00a0\u2007\u202f\u3000"
SHA = re.compile(r"^[0-9a-f]{40}$")
USES = re.compile(r"^\s*uses:\s*(\S+)\s*$", re.MULTILINE)
# Тег генератора SLSA: его workflow нельзя пинить на SHA из-подписи.
ALLOWED_TAG_USES = re.compile(
    r"^slsa-framework/slsa-github-generator/\.github/workflows/"
    r"generator_generic_slsa3\.yml@v\d+\.\d+\.\d+$")


def check(path: Path) -> list[str]:
    errors: list[str] = []
    text = path.read_text(encoding="utf-8")

    for index, line in enumerate(text.splitlines(), 1):
        for char in line:
            if char in INVISIBLE:
                errors.append(f"{path.name}:{index}: невидимый символ "
                              f"U+{ord(char):04X}")

    for match in USES.finditer(text):
        ref = match.group(1)
        if ref.startswith("./"):
            continue  # локальный reusable workflow
        at = ref.rfind("@")
        if at < 0:
            errors.append(f"{path.name}: uses без версии: {ref}")
            continue
        pinned = ref[at + 1:]
        if SHA.match(pinned) or ALLOWED_TAG_USES.match(ref):
            continue
        errors.append(f"{path.name}: не закреплено: {ref} "
                      f"(нужен SHA-коммит или разрешённый тег)")

    # permissions: верхний уровень есть и стоит до jobs (чтобы читался
    # как глобальный, а не как потом добавленная мысль).
    perm = re.search(r"^permissions:", text, re.MULTILINE)
    if not perm:
        errors.append(f"{path.name}: нет блока permissions на верхнем уровне")
    else:
        jobs_at = re.search(r"^jobs:", text, re.MULTILINE)
        if jobs_at and perm.start() > jobs_at.start():
            errors.append(f"{path.name}: permissions стоит ПОСЛЕ jobs")

    if not re.search(r"^name:", text, re.MULTILINE):
        errors.append(f"{path.name}: нет name")
    if not re.search(r"^on:", text, re.MULTILINE):
        errors.append(f"{path.name}: нет on")
    return errors


def main() -> int:
    directory = Path(__file__).resolve().parent.parent / ".github" / "workflows"
    files = sorted(directory.glob("*.yml"))
    if not files:
        print("воркфлоу не найдены:", directory)
        return 1
    errors: list[str] = []
    for path in files:
        errors += check(path)
        print(f"проверен: {path.name}")
    if errors:
        print("\nОШИБКИ:")
        for error in errors:
            print("  -", error)
        return 1
    print(f"OK: {len(files)} воркфлоу, пины и permissions на месте")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
