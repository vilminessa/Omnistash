"""Сборка клиентского бандла (docs/SERVER-CLIENT.md §3, §6, §9 - шаг S7).

Бандл - ровно то, что отдаёт слушатель клиента (--serve): страница главного
окна, страница воспроизведения, CSS, JS плеера и вендор `hls.js` с пином по
sha256. Ничего не скачивается: файлы читаются из репозитория, несоответствие
пина называется причиной, а не «тихо скопировали то, что есть».

    python tools/build_client.py           # собрать client_dist/ и manifest
    python tools/build_client.py --check   # проверить пины и чужие адреса, не писать
    python tools/build_client.py --zip     # дополнительно client_dist/omnistash-client.zip

Метку сервера (`window.OMNISTASH_HTTP`) ставит слушатель при раздаче страницы
(`app/ui.py:build_page(marker=...)`), поэтому в бандле страницы без метки: это
тот же текст, который сервер отдаёт клиенту, а не отдельная версия UI.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import i18n, ui  # noqa: E402
from app.http.server import CLIENT_ASSETS  # noqa: E402

DIST = ROOT / "client_dist"
ZIP_NAME = "omnistash-client.zip"
VENDOR_NAME = "vendor/hls.js"
VENDOR_META = "vendor/hls.js.meta.json"
VENDOR_LICENSE = "vendor/hls.js.LICENSE.txt"
# Что грузит рантайм клиента со своего адреса (§6): скрипт плеера и вендор.
EXTERNAL_LOADS = ("/app.css", "/watch.js", "/vendor/hls.js")

# Чужой адрес в рантайме = supply chain (§10). Проверка по коду, не по
# комментариям: комментарии называют площадки и ссылки текстом. Строки `//`
# снимаются отдельно от block-комментов и БЕЗ re.S: с re.S `.*$` съедает
# остаток файла и проверка становится пустой.
FETCH_ABSOLUTE = re.compile(r'fetch\(\s*["\']https?://', re.I)
TAG_ABSOLUTE = re.compile(r'<(?:script|link)\b[^>]*\b(?:src|href)\s*=\s*["\']https?://',
                          re.I)
BLOCK_COMMENT = re.compile(r"/\*.*?\*/|<!--.*?-->", re.S)
LINE_COMMENT = re.compile(r"^\s*//.*$", re.M)


def sha256_bytes(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def read_source(name: str) -> bytes:
    """Файл из ui_src. Отсутствие называется причиной (в заморозке часть
    файлов может не быть - бандл тогда не собирается молча)."""
    try:
        return ui.asset(name)
    except Exception as exc:  # noqa: BLE001 - файл не в источнике
        raise ValueError(f"файл клиента не найден ({name}): {exc}") from exc


def bundle_pages() -> dict[str, bytes]:
    """Страницы бандла: главная и воспроизведения, обе без метки сервера."""
    pages = {"index.html": ui.build_page().encode("utf-8")}
    if not ui.watch_page_available():
        raise ValueError("страница воспроизведения не найдена (ui_src/watch.html): "
                         "клиент не получит /watch/<video_id>")
    pages[ui.WATCH_PAGE] = ui.build_watch_page().encode("utf-8")
    return pages


def bundle_assets() -> dict[str, bytes]:
    """Файлы по карте раздачи (один источник правды - CLIENT_ASSETS)."""
    out = {}
    for route, (name, _ctype) in CLIENT_ASSETS.items():
        out[name] = read_source(name)
    return out


def vendor_pin() -> tuple[str, dict]:
    """Пин вендора: sha256 файла сверяется с метадатой пакета (§6).

    Текст лицензии Apache-2.0 лежит рядом и тоже сверяется по sha256: без
    текста лицензии распространение вендора было бы неполным (§4 Apache-2.0),
    а «we copied the file» без лицензии - враньём о составе бандла.
    """
    meta = json.loads(read_source(VENDOR_META).decode("utf-8"))
    body = read_source(VENDOR_NAME)
    actual = sha256_bytes(body)
    expected = str(meta.get("sha256") or "")
    if actual != expected:
        raise ValueError(f"пин вендора не совпадает: {VENDOR_NAME} "
                         f"файл {actual}, метадата {expected}")
    if str(meta.get("license") or "") != "Apache-2.0":
        raise ValueError(f"лицензия вендора не Apache-2.0: "
                         f"{meta.get('license')!r} ({VENDOR_NAME})")
    license_body = read_source(VENDOR_LICENSE)
    license_expected = str(meta.get("licenseSha256") or "")
    license_actual = sha256_bytes(license_body)
    if license_actual != license_expected:
        raise ValueError(f"пин текста лицензии не совпадает: {VENDOR_LICENSE} "
                         f"файл {license_actual}, метадата {license_expected}")
    return actual, meta


def code_only(text: str) -> str:
    """Код без комментариев: block-комменты и строки `//` снимаются по-разному."""
    return LINE_COMMENT.sub("", BLOCK_COMMENT.sub("", text))


def external_targets(name: str, body: bytes) -> list[str]:
    """Абсолютные адреса в рантайме страницы или скрипта - чужие хосты (§10)."""
    code = code_only(body.decode("utf-8", "replace"))
    hits = []
    if name.endswith(".html"):
        for match in TAG_ABSOLUTE.finditer(code):
            hits.append(match.group(0)[:70])
    if FETCH_ABSOLUTE.search(code):
        hits.append("fetch на абсолютный адрес")
    return hits


def watch_page_loads(html: str) -> list[str]:
    """Страница плеера ссылается только на свой адрес (§6)."""
    code = code_only(html)
    missing = [route for route in EXTERNAL_LOADS if f'"{route}"' not in code]
    return missing


def build(check_only: bool, want_zip: bool) -> tuple[int, list[str]]:
    errors: list[str] = []
    try:
        pages = bundle_pages()
        assets = bundle_assets()
        digest, meta = vendor_pin()
    except ValueError as exc:
        return 1, [str(exc)]

    for name, body in pages.items():
        hits = external_targets(name, body)
        if hits:
            errors.append(f"{name}: чужой адрес в разметке - {hits}")
    for name, body in assets.items():
        hits = external_targets(name, body)
        if hits:
            errors.append(f"{name}: чужой адрес в коде - {hits}")
    missing = watch_page_loads(pages[ui.WATCH_PAGE].decode("utf-8"))
    if missing:
        errors.append(f"{ui.WATCH_PAGE}: плеер не ссылается на свой адрес: "
                      f"{missing} (рантайм грузит только свой адрес, §6)")
    if i18n.t("pair.submit", "ru") == "" or i18n.t("expose.title", "ru") == "":
        errors.append("словарь подписей не покрывает привязку и согласие (§8)")

    manifest = {
        "tool": "tools/build_client.py",
        "note": ("бандл читается из репозитория, из сети ничего не "
                 "скачивается; метку сервера ставит --serve при раздаче "
                 "страницы"),
        "pages": {name: {"bytes": len(body), "sha256": sha256_bytes(body)}
                  for name, body in pages.items()},
        "assets": {name: {"bytes": len(body), "sha256": sha256_bytes(body)}
                   for name, body in assets.items()},
        "vendor": {"file": VENDOR_NAME, "sha256": digest, "package": meta["package"],
                   "version": meta["version"], "license": meta["license"],
                   "licenseFile": VENDOR_LICENSE,
                   "licenseSha256": str(meta.get("licenseSha256") or ""),
                   "licenseSource": meta.get("licenseSource", ""),
                   "source": meta.get("source", ""), "fetched": meta.get("fetched", ""),
                   "publish": meta.get("publish", "")},
        "served_routes": sorted(CLIENT_ASSETS),
    }

    if errors:
        return 1, errors
    if check_only:
        print(f"бандл проверен: страниц {len(pages)}, файлов раздачи "
              f"{len(assets)}, вендор {meta['package']} {meta['version']} "
              f"({meta['license']}), пин {digest[:16]}... и пин текста лицензии "
              f"{meta['licenseSha256'][:16]}... - совпадают")
        return 0, []

    DIST.mkdir(exist_ok=True)
    for name, body in pages.items():
        (DIST / name).write_bytes(body)
    for name, body in assets.items():
        target = DIST / name
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(body)
    (DIST / VENDOR_META).write_bytes(read_source(VENDOR_META))
    (DIST / VENDOR_LICENSE).write_bytes(read_source(VENDOR_LICENSE))
    (DIST / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    total = sum(len(body) for body in pages.values()) + \
        sum(len(body) for body in assets.values())
    print(f"собрано: {DIST} ({len(pages)} страницы, {len(assets)} файла раздачи, "
          f"{total} байт плюс вендор, метадата и текст лицензии)")
    if want_zip:
        zip_path = DIST / ZIP_NAME
        # Список файлов берётся до создания архива и без самого архива:
        # иначе zipfile читает собственный растущий файл - сборка виснет.
        # Путь внутри архива - relative к бандлу: вендор остаётся в vendor/,
        # иначе клиентский путь /vendor/hls.js в архиве стал бы просто hls.js.
        members = [name for name in sorted(DIST.rglob("*"))
                   if name.is_file() and name != zip_path]
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as pack:
            for name in members:
                pack.write(name, name.relative_to(DIST).as_posix())
        print(f"архив: {zip_path} ({zip_path.stat().st_size} байт)")
    return 0, []


def main(argv: list[str]) -> int:
    code, errors = build("--check" in argv, "--zip" in argv)
    for line in errors:
        print(f"отказ: {line}", file=sys.stderr)
    if code:
        print("бандл не собран: причины названы выше - молчания нет", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
