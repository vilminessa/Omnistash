"""Mesh-клиент: манифесты участников и загрузка файла с резюме.

Порядок (см. verify.review): адресаты -> подписанные манифесты ->
выбор хеша -> загрузка в .part с дозапиской -> своя проверка. Клиент
НЕ принимает решений за человека: он собирает факты, фильтрует
стоп-лист и подписи, а барьеры - у verify.review.

Транспорт - только TLS (mesh.channel): аттестация сертификата пира
верифицируется ДО первого байта запроса (в connect), запрос с id
видео уходит уже в зашифрованном канале по подтверждённому ключу.
Манифест обязан быть подписан ТЕМ же ключом, что открыл канал, -
иначе пара отбрасывается.

Сетевые сбои адресатов не фатальны: недоступный участник просто
выпадает из выборки (у нас же могли не ответить все).
"""

from __future__ import annotations

import http.client
import json
import ssl
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from . import catalog as catalog_mod
from . import channel as channel_mod
from . import crypt as crypt_mod
from . import manifest as manifest_mod

CONNECT_TIMEOUT = 6.0
READ_TIMEOUT = 60.0          # большие файлы читаем чанками долго
CHUNK = 256 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_CATALOG_BYTES = 4 * 1024 * 1024
USER_AGENT = "OmnistashMesh/1"


class MeshCancelled(Exception):
    """Остановлено пользователем (stop-событие очереди/окна)."""


def _connect(addr: str, timeout: float, expect: str | None,
             pin: bool = True):
    """TLS-соединение к участнику; проверка аттестации В connect().

    http.client шлёт запрос только после conn.connect() - значит
    verify_peer внутри переопределённого connect() гарантированно
    выполняется ДО первого байта (в спайке это подтверждено счётчиком
    обращений сервера: при отказе - ноль). pin=False - проверить ключ,
    но НЕ записывать пин (поиск участников так и делает).
    """
    host, _, port = str(addr).partition(":")
    conn = http.client.HTTPSConnection(
        host, int(port or 443), timeout=timeout,
        context=channel_mod.client_context())
    base_connect = conn.connect
    try:
        base_connect()
    except ssl.SSLError as exc:
        conn.close()
        raise channel_mod.ChannelError(
            f"{addr} не отвечает на шифрованный канал (возможно, старая "
            f"версия участника): {exc}") from exc
    try:
        conn.mesh_identity = channel_mod.verify_peer(
            conn.sock.getpeercert(binary_form=True), expected=expect,
            addr=addr if pin else None)
    except channel_mod.ChannelError:
        conn.close()
        raise
    return conn


def _request(addr: str, path: str, *, timeout: float = CONNECT_TIMEOUT,
             headers: dict | None = None,
             expect: str | None = None):
    """GET по TLS -> (conn, response); вызывающий обязан закрыть conn."""
    conn = _connect(addr, timeout, expect)
    try:
        conn.request("GET", path,
                     headers={"User-Agent": USER_AGENT, **(headers or {})})
        return conn, conn.getresponse()
    except Exception:
        conn.close()
        raise


def collect_manifests(remote_id: str, addrs: list[str],
                      expects: dict | None = None) -> list[tuple]:
    """Манифесты всех адресатов: [(addr, manifest)], сбой - молча мимо.

    Подписи здесь НЕ проверяются - это делает verify.review, чтобы
    недействительные заявления просто не участвовали в счётчиках.
    ЕДИНСТВЕННОЕ правило здесь: ключ подписи манифеста должен совпасть
    с ключом, открывшим TLS-канал - чужой канал с чужим манифестом
    (или подменённый манифест) отбрасываются.
    expects: {addr: pubkey_hex} - ожидаемый ключ (DHT/пины/друзья).
    """
    expects = expects or {}

    def one(addr: str):
        conn, response = _request(addr, f"/manifest/{remote_id}",
                                  timeout=CONNECT_TIMEOUT,
                                  expect=expects.get(addr))
        try:
            if response.status != 200:
                response.read()
                raise RuntimeError(f"участник ответил {response.status}")
            raw = response.read(MAX_MANIFEST_BYTES)
        finally:
            conn.close()
        item = json.loads(raw.decode("utf-8"))
        channel_key = getattr(conn, "mesh_identity", "")
        if channel_key and str(item.get("pubkey") or "") != channel_key:
            raise RuntimeError("манифест подписан другим ключом, чем канал")
        return addr, item

    out: list[tuple] = []
    if not addrs:
        return out
    with ThreadPoolExecutor(max_workers=min(8, len(addrs))) as pool:
        for future in [pool.submit(one, addr) for addr in addrs]:
            try:
                out.append(future.result())
            except Exception:  # noqa: BLE001 - участник недоступен/соврал
                continue
    return out


def pick_digest(pairs: list[tuple], banned=frozenset()) -> str | None:
    """Хеш для загрузки: мажоритарный среди подписанных незабаненных."""
    counts: Counter = Counter()
    for _addr, item in pairs:
        if manifest_mod.verify(item) is not None:
            continue
        digest = str(item.get("sha256") or "")
        if not digest or digest in banned:
            continue
        counts[digest] += 1
    return counts.most_common(1)[0][0] if counts else None


def collect_catalogs(addrs: list[str],
                     expects: dict | None = None) -> list[tuple]:
    """Каталоги адресатов (B3): [(addr, pubkey, чистые записи)].

    Сбой одного адресата не валит остальных (как в манифестах). Ключ
    канала обязан совпасть с ключом подписи каталога: чужой каталог,
    подсунутый по нашему каналу, отбрасывается.
    """
    expects = expects or {}

    def one(addr: str):
        conn, response = _request(addr, "/catalog",
                                  timeout=CONNECT_TIMEOUT,
                                  expect=expects.get(addr))
        try:
            if response.status != 200:
                response.read()
                raise RuntimeError(f"участник ответил {response.status}")
            raw = response.read(MAX_CATALOG_BYTES)
        finally:
            conn.close()
        try:
            snapshot = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise RuntimeError("каталог пришёл не JSON") from exc
        channel_key = str(getattr(conn, "mesh_identity", "") or "")
        if str((snapshot or {}).get("pubkey") or "") != channel_key:
            raise RuntimeError("каталог подписан другим ключом, чем канал")
        entries, error = catalog_mod.verify(snapshot)
        if error:
            raise RuntimeError(f"каталог не прошёл проверку: {error}")
        return addr, channel_key, entries or []

    out: list[tuple] = []
    if not addrs:
        return out
    with ThreadPoolExecutor(max_workers=min(8, len(addrs))) as pool:
        for future in [pool.submit(one, addr) for addr in addrs]:
            try:
                out.append(future.result())
            except Exception:  # noqa: BLE001 - участник недоступен/соврал
                continue
    return out


def probe(addr: str, expect: str | None = None,
          timeout: float = CONNECT_TIMEOUT, pin: bool = True) -> str:
    """Открыть канал и тут же закрыть: проверка адреса/ключа без запросов.

    Кнопка «Проверить адрес» (pin=True - ключ запоминается) и поиск
    участников (pin=False - только отпечаток): handshake + аттестация
    в connect, никаких id наружу. Возвращает ключ, открывший канал;
    отказы - ChannelError/OSError наверх.
    """
    conn = _connect(addr, timeout, expect, pin=pin)
    try:
        return str(getattr(conn, "mesh_identity", ""))
    finally:
        conn.close()


def fetch(remote_id: str, addr: str, part_path, *, progress=None,
          stop=None, expect: str | None = None,
          encrypt_to: bytes | None = None) -> dict:
    """Загрузка (или дозагрузка) в part_path. Возвращает
    {resumed_from, written, total} - хеш считает вызывающая сторона.

    expect - ключ, которым открыт канал (обычно pubkey из манифеста,
    по которому выбрали этого адресата): запрос id идёт только после
    прохождения аттестации.
    encrypt_to - X25519-ключ (32 байта): файл уходит шифроблоками под
    него (B2), читается только нашим ключом; в этом режиме дозапись
    не поддерживается - начинается с нуля.
    """
    import os
    existing = 0
    path = f"/file/{remote_id}"
    if encrypt_to:
        path += f"?enc=v1&pk={bytes(encrypt_to).hex()}"
    elif os.path.exists(part_path):
        existing = os.path.getsize(part_path)
    headers = {"Range": f"bytes={existing}-"} if existing else {}
    mode = "wb"
    conn = None
    try:
        conn, response = _request(addr, path,
                                  timeout=CONNECT_TIMEOUT, headers=headers,
                                  expect=expect)
        status = getattr(response, "status", 200)
        if status not in (200, 206):
            detail = response.read(300).decode("utf-8", "replace")
            raise RuntimeError(f"участник ответил {status}: {detail[:120]}")
        if encrypt_to:
            envelope = crypt_mod.parse_envelope(
                response.headers.get(crypt_mod.HEADER))
            if envelope is None:
                raise RuntimeError("участник не поддержал шифрование "
                                   "(старая версия?)")

            def enc_progress(done: int, _total: int) -> None:
                if stop is not None and stop.is_set():
                    raise MeshCancelled("остановлено пользователем")
                if progress:
                    progress(done, _total)

            written = crypt_mod.open_stream(part_path, response, envelope,
                                            progress=enc_progress)
            return {"resumed_from": 0, "written": written}
        if existing and status != 206:
            # Участник проигнорировал Range: начинаем заново.
            existing = 0
            mode = "wb"
        else:
            mode = "ab" if existing else "wb"
        declared = response.headers.get("Content-Length")
        total = existing + int(declared) if declared else None
        written = 0
        with open(part_path, mode) as out:
            while True:
                if stop is not None and stop.is_set():
                    raise MeshCancelled("остановлено пользователем")
                chunk = response.read(CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                written += len(chunk)
                if progress:
                    progress(existing + written, total)
    except MeshCancelled:
        raise
    except Exception as exc:  # noqa: BLE001 - причину показывает вызывающий
        raise RuntimeError(f"перенос с {addr} не удался: {exc}") from exc
    finally:
        if conn is not None:
            conn.close()
    return {"resumed_from": existing, "written": written}


def sniffer_ext(path) -> str:
    """Расширение файла по сигнатуре содержимого (не по чужим словам).

    Имя мы формируем САМИ (шаблон с [id]), поэтому стороннее расширение
    не используется - оно лишь подсказка; а подделать «внутренности»
    всё равно не даёт проверка хеша.
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(16)
    except OSError:
        return "mp4"
    if len(head) >= 8 and head[4:8] == b"ftyp":
        return "mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        # EBML: matroska/webm - разводим по DocType внутри (первые КБ).
        try:
            with open(path, "rb") as handle:
                blob = handle.read(4096)
            return "webm" if b"webm" in blob else "mkv"
        except OSError:
            return "mkv"
    if head.startswith(b"OggS"):
        return "ogg"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "webp"
    return "mp4"   # YouTube по умолчанию отдаёт mp4
