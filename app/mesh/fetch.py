"""Восстановление файла у участников: один путь для очереди и карточки.

Порядок (см. mesh.verify): адресаты из настроек -> подписанные
манифесты -> мажоритарный хеш (стоп-лист отсекает «несовпадения»
прошлого) -> решение review (один узел достаточно; эталон моей базы и
стоп-лист - барьеры) -> перенос в .part с дозапиской -> sha256 ->
структурная сверка длительности со СВОЕЙ строкой -> имя по нашему
шаблону (с [id], расширение по сигнатуре) -> запись с провенансом
в files.format_json.

Метаданные от участников не принимаются НИКОГДА: свои (synced_at) - и
есть сверяемый факт.
"""

from __future__ import annotations

import json
import os

from ..indexer import file_hash
from ..util import human_size, sanitize_name
from . import client, crypt, dht, feedback, manifest, verify


def _drop_revoked(conn, pairs, log):
    """Манифесты отозванных устройств (truststore) не участвуют дальше.

    Отзыв обязан касаться и будущих файлов: и мажоритарный хеш, и выбор
    адресата считаются только по незабаненным устройствам - иначе
    «отозвать доверие» было бы чисто декоративным жестом.
    """
    from .. import repo
    revoked = repo.truststore_revoked(conn)
    if not revoked:
        return pairs
    kept = [(addr, item) for addr, item in pairs
            if str(item.get("pubkey") or "") not in revoked]
    dropped = len(pairs) - len(kept)
    if dropped:
        log(f"Mesh: пропущено адресатов с отозванным доверием: {dropped}")
    return kept


def restore(conn, row: dict, settings: dict, dest_dir, *,
            log=None, stop=None, reason: str = "") -> bool:
    """Попытка получить файл строки у участников. True - файл получен,
    проверен и записан (строка становится скачанной).

    row: {"id", "remote_id", "title"}; dest_dir - уже разрешённая папка
    назначения (вызывающая сторона спрашивает хранилище сама).
    """
    log = log or (lambda message: None)

    def give_up(message: str) -> bool:
        log(message)
        return False

    # Адреса участников: "ip:port" или "ip:port@ключ" (ключ - сразу пин
    # для TLS-канала: запрос id уйдёт только по подтверждённой личности).
    expects: dict = {}
    addrs: list = []
    for entry in str(settings.get("mesh_peers") or "") \
            .replace(";", ",").split(","):
        entry = entry.strip()
        if not entry:
            continue
        addr, _, expect = entry.partition("@")
        addr = addr.strip()
        if not addr:
            continue
        addrs.append(addr)
        if expect.strip():
            expects[addr] = expect.strip().lower()
    banned = feedback.banned_set(conn)
    # DHT: участники, объявившие этот файл (плюс ручные адреса). Сеть
    # отдаёт только АДРЕСА (ядро BEP5): ключ/подпись/хеш придут с
    # терминала в манифесте по TLS, а фильтр стоп-листа сработает при
    # выборе хеша (pick_digest) - предфильтровать по чужому слову нечего.
    node = dht.current()
    if node is not None and node.running:
        for found in node.lookup_sync(row["remote_id"]):
            addr = str(found.get("addr") or "")
            if not addr or addr in addrs:
                continue
            addrs.append(addr)
    if not addrs:
        return give_up("Mesh: адреса участников не заданы - пропускаю "
                       f"(«{row.get('title') or row.get('key')}»)")
    meta = conn.execute(
        "SELECT synced_at, duration_s, title FROM videos WHERE id=?",
        (row["id"],)).fetchone()
    if not meta or not meta["synced_at"]:
        return give_up("Mesh: у строки нет своих метаданных (синк не был) - "
                       "сначала «Обновить метаданные»")
    own_row = conn.execute(
        "SELECT hash FROM files WHERE video_id=? AND kind='video' "
        "AND hash IS NOT NULL ORDER BY id LIMIT 1",
        (row["id"],)).fetchone()
    own_digest = own_row["hash"] if own_row else None

    log(f"Mesh: ищу «{row.get('title') or row.get('key')}» "
        f"у {len(addrs)} адресатов" + (f" (причина: {reason})" if reason else ""))
    try:
        pairs = client.collect_manifests(row["remote_id"], addrs,
                                         expects=expects or None)
    except Exception as exc:  # noqa: BLE001 - сбой не должен ронять поток
        return give_up(f"Mesh: манифесты не получены: {exc}")
    pairs = _drop_revoked(conn, pairs, log)

    digest = client.pick_digest(pairs, banned)
    if not digest:
        return give_up("Mesh: нет пригодных манифестов (недоступны, "
                       "без подписи или в стоп-листе)")
    # Репутация: сколько раз ключи-носители принятого хеша УЖЕ приносили
    # «несовпадение». Информация для человека (в review и в провенанс),
    # барьером не становится - блокирует только стоп-лист по хешу.
    holders = {str(m.get("pubkey") or "")
               for m in (item for _addr, item in pairs)
               if m.get("sha256") == digest and manifest.verify(m) is None}
    offenses = sum(feedback.peer_offenses(conn, key) for key in holders if key)
    if offenses:
        log(f"Mesh: внимание - узлы с этим хешом уже приносили несовпадения "
            f"({offenses}); в карточке будет пометка")
    decision = verify.review(
        digest, own_digest=own_digest,
        manifests=[item for _addr, item in pairs], banned=banned,
        offenses=offenses)
    if not decision["ok"]:
        return give_up(f"Mesh: отказ - {decision['reason']}")
    peer, item = next(
        ((addr, m) for addr, m in pairs
         if m.get("sha256") == digest and manifest.verify(m) is None),
        (None, None))
    if not peer:
        return give_up("Mesh: адресат с принятым хешом недоступен")

    part = os.path.join(dest_dir, f".{row['remote_id']}.mesh.part")
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError as exc:
        return give_up(f"Mesh: папка назначения недоступна: {exc}")
    log(f"Mesh: переношу с {peer} - {decision['level']}, "
        f"подтверждений {decision['consensus']}")
    try:
        # expect = ключ манифеста, по которому выбрали адресата: канал
        # должен открыться именно им (TOFU-пин подтвердится снаружи).
        # mesh_encrypt: шифроблоки под СВОЙ ключ (B2) - прочитать байты
        # по пути сможет только обладатель нашего ключа.
        encrypt_to = (crypt.own_x25519_public()
                      if settings.get("mesh_encrypt") else None)
        if encrypt_to:
            log("Mesh: перенос шифруется под мой ключ (блоки 128 КБ)")
        client.fetch(row["remote_id"], peer, part, stop=stop,
                     expect=str(item.get("pubkey") or "") or None,
                     encrypt_to=encrypt_to)
    except client.MeshCancelled:
        return give_up("Mesh: перенос остановлен")
    except Exception as exc:  # noqa: BLE001
        try:
            os.remove(part)
        except OSError:
            pass
        return give_up(f"Mesh: перенос не удался: {exc}")

    # Проверка 1: содержимое ровно то, что заявлено.
    try:
        got = file_hash(part)
    except OSError as exc:
        return give_up(f"Mesh: не удалось прочитать файл: {exc}")
    if got != digest:
        try:
            os.remove(part)
        except OSError:
            pass
        return give_up(f"Mesh: хеш не сошёлся (заявлено {digest[:20]}…, "
                       f"получено {got[:20]}…) - отбрасываю")

    # Проверка 2: структура против СВОЕЙ строки (длительность).
    structure = verify.probe(part, meta["duration_s"])
    if structure["ok"] is False:
        try:
            os.remove(part)
        except OSError:
            pass
        return give_up("Mesh: структурная проверка не прошла: "
                       + "; ".join(structure["errors"]))

    # Имя формируем САМИ по шаблону (с [id] - скан опознаёт), расширение -
    # по сигнатуре содержимого, не по чужим словам.
    ext = client.sniffer_ext(part)
    title = sanitize_name(meta["title"] or row["remote_id"])
    final = os.path.join(dest_dir, f"{title} [{row['remote_id']}].{ext}")
    try:
        os.replace(part, final)
        stat = os.stat(final)
    except OSError as exc:
        return give_up(f"Mesh: не удалось положить файл: {exc}")

    provenance = verify.provenance(
        decision, peer_pubkey=str(item.get("pubkey") or ""),
        peer_synced_at=str(item.get("synced_at") or ""),
        structure=structure)
    from .. import repo
    repo.record_file(conn, row["id"], final, "video",
                     size=stat.st_size, mtime=stat.st_mtime, digest=got,
                     format_json=json.dumps(provenance, ensure_ascii=False))
    # record_file переводит в downloaded не все статусы (unavailable среди
    # них нет) - архивную строку восстанавливаем явно.
    status = conn.execute("SELECT status FROM videos WHERE id=?",
                          (row["id"],)).fetchone()
    if status and status["status"] != "downloaded":
        repo.set_status(conn, [row["id"]], "downloaded")
    repo.set_last_error(conn, row["id"], None)
    log(f"Mesh: восстановлено - {os.path.basename(final)} "
        f"({decision['level']}, {human_size(stat.st_size)}, "
        f"подтверждений {decision['consensus']}, синк узла "
        f"{provenance['peer_synced_at'] or 'не указан'})")
    return True
