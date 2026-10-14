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
from . import client, crypt, dht, feedback, manifest, pacing, verify


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


def _strict_filter(conn, settings, pairs, log):
    """Strict (1.2): манифесты - только от подтверждённых устройств.

    Режим включается автоматически при первом подтверждённом друге
    (gui.trust_sas_confirm / trust_op verify) и выключается только
    человеком явно. Барьер стоит на манифестах и файлах; информационные
    каталоги strict не трогает.
    """
    if not settings.get("mesh_strict"):
        return pairs
    from .. import repo
    trusted = repo.truststore_verified(conn)
    kept = [(addr, item) for addr, item in pairs
            if str(item.get("pubkey") or "") in trusted]
    dropped = len(pairs) - len(kept)
    if dropped:
        log(f"Mesh: strict - пропущено манифестов от неподтверждённых "
            f"устройств: {dropped} (сверьте отпечаток или 12 слов в "
            "диалоге «Участники»)")
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
    # F5: последний рабочий адрес доверенного устройства - в очередь до
    # DHT-перебора: он уже когда-то отдавал этот файл (проверенный путь),
    # а сеть могла его не знать (переезд, истёкший TTL). Ожидание ключа
    # по адресу: личность подтверждается по TLS уже с первой пробы.
    from .. import repo
    for device in repo.truststore_list(conn):
        if device.get("revoked_at"):
            continue                    # отзыв - жёсткий барьер
        addr = str(device.get("last_addr") or "").strip()
        if not addr or addr in addrs:
            continue
        addrs.append(addr)
        expects.setdefault(addr, str(device.get("pubkey") or "").lower())
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
    # Редиал дружбы (1.3): свежие адреса подтверждённых устройств из
    # адресной книги - смена IP друга не должна рвать восстановление
    # (критерий приёмки этапа 1: «смена IP восстанавливается сама»).
    from .. import repo
    for key in repo.truststore_verified(conn):
        for candidate in repo.peer_addr_list(conn, key, limit=3):
            addr = str(candidate.get("addr") or "")
            if addr and addr not in addrs:
                addrs.append(addr)
                expects.setdefault(addr, key)
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
    pairs = _strict_filter(conn, settings, pairs, log)

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
        # Лимиты B1.7: размер и скорость - из настроек (0 = без ограничения);
        # отказ по размеру происходит ДО записи part.
        client.fetch(row["remote_id"], peer, part, stop=stop,
                     expect=str(item.get("pubkey") or "") or None,
                     encrypt_to=encrypt_to,
                     max_bytes=pacing.mb_to_bytes(
                         settings.get("mesh_max_mb")),
                     rate_bps=pacing.mbit_to_bps(
                         settings.get("mesh_rate_mbit")))
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
    if str(item.get("pubkey") or ""):
        try:
            # адрес сработал - фиксируем для редиала (1.3)
            repo.peer_addr_add(conn, str(item["pubkey"]), peer, "fetch")
        except ValueError:
            pass                            # кривой ключ в манифесте
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


def restore_friend(conn, catalog_row: dict, addr: str, dest_dir, *,
                   log=None, stop=None) -> bool:
    """Забрать файл у друга по ЧУЖОЙ строке (этап 2.1, §6(б)).

    Продуктовое правило B3 остаётся в силе: строка в индекс НЕ
    заводится, чужой title используется только для ИМЕНИ файла.
    Проверки полученных данных (критерий заказчика):
      * хеш берётся из ПОДПИСАННОГО манифеста ключом автора каталога
        (origin) - ключ канала обязан совпасть с ним же;
      * sha256 скачанного сверяется с манифестом;
      * структурная проверка сигнатуры без эталона (чужой duration
        не принимаем - сверять нечего, кроме самого контейнера).
    Провенанс (кто отдал, чей хеш, origin=friend) пишется пофайловым
    sidecar'ом рядом с файлом.
    """
    log = log or (lambda message: None)
    remote_id = str(catalog_row.get("remote_id") or "")
    origin = str(catalog_row.get("origin") or "").lower()
    title = str(catalog_row.get("title") or "") or remote_id
    if not remote_id or not origin or not addr:
        return False
    banned = feedback.banned_set(conn)
    expects = {addr: origin}
    pairs = client.collect_manifests(remote_id, [addr], expects=expects)
    # Только манифесты АВТОРА каталога: подпись ключом origin, без
    # битых подписей; отзыв доверия отсекается штатным фильтром.
    pairs = [(a, item) for a, item in pairs
             if str(item.get("pubkey") or "").lower() == origin
             and manifest.verify(item) is None]
    pairs = _drop_revoked(conn, pairs, log)
    digest = client.pick_digest(pairs, banned)
    if not digest:
        log(f"Mesh: у друга нет пригодного манифеста «{title}» "
            "(стоп-лист/подпись/недоступен)")
        return False
    from ..util import now_iso
    part = os.path.join(str(dest_dir), f".friend_{remote_id}.part")
    try:
        client.fetch(remote_id, addr, part, expect=origin)
        got = file_hash(part)
        if got != digest:
            raise RuntimeError(f"хеш не сошёлся (заявлен {digest[:16]}…, "
                               f"получено {got[:16]}…)")
        structure = verify.probe(part, None)
        if structure["ok"] is False:
            raise RuntimeError("структурная проверка: "
                               + "; ".join(structure["errors"]))
        ext = client.sniffer_ext(part)
        final = os.path.join(dest_dir,
                             f"{sanitize_name(title)} [{remote_id}].{ext}")
        os.replace(part, final)
    except Exception as exc:  # noqa: BLE001 - честный отказ в журнал
        try:
            os.remove(part)
        except OSError:
            pass
        log(f"Mesh: забор у друга не удался «{title}»: {exc}")
        return False
    sidecar = os.path.splitext(final)[0] + ".post.json"
    try:
        with open(sidecar, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "origin": "friend", "from": origin,
                "remote_id": remote_id, "title": title,
                "sha256": digest, "size": os.path.getsize(final),
                "fetched_at": now_iso(), "path": final,
            }, ensure_ascii=False, indent=1))
    except OSError:
        pass                      # провенанс - не барьер для получения
    log(f"Mesh: забрано у друга «{title}» ({human_size(os.path.getsize(final))}, "
        f"sha {digest[:16]}… сверен, провенанс origin=friend)")
    return True
