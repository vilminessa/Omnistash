"""Mesh-M2: сервер участника, клиент с резюме, восстановление целиком.

Сеть - локальная (127.0.0.1, системный порт): «два участника» живут в
одной прогонке - у каждого своя база, как у настоящих машин. Структурная
сверка (probe) в интеграции подменяется: её логика покрыта в test_mesh,
здесь важен сам путь переноса и проверки.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import db as db_mod, repo
from app.mesh import channel as channel_mod
from app.mesh import catalog as catalog_mod
from app.mesh import client, fetch as mesh_fetch, identity, manifest, server
from app.mesh import crypt as crypt_mod
from app.mesh import verify as mesh_verify


class MeshNetCase(unittest.TestCase):
    """Два узла: А (владеет файлом) и Б (восстанавливает)."""

    def setUp(self):
        # Идентичность - свежая на тестовый профиль (подписи манифестов).
        self._prof = tempfile.TemporaryDirectory()
        self.addCleanup(self._prof.cleanup)
        for module in (identity, channel_mod):
            # Каждый модуль берёт profile_dir именем из paths - патчим
            # оба: пины TLS (channel) не должны уезжать в настоящий профиль.
            patcher = mock.patch.object(module, "profile_dir",
                                        lambda: Path(self._prof.name))
            patcher.start()
            self.addCleanup(patcher.stop)
        identity.reset_cache()
        self.addCleanup(identity.reset_cache)
        # Пины TLS-канала живут в профиле - между тестами не должны течь.
        channel_mod.reset_cache()
        self.addCleanup(channel_mod.reset_cache)

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db_a = db_mod.Database(self.root / "a.db")
        self.db_b = db_mod.Database(self.root / "b.db")
        self.addCleanup(self.db_a.close)
        self.addCleanup(self.db_b.close)
        self.server = None

    def tearDown(self):
        if self.server is not None:
            self.server.stop()

    def raw_get(self, path: str, headers: dict | None = None,
                addr: str | None = None):
        """TLS-GET в обход пинов - прямая проверка ответов сервера.

        (Пин-логику проверяет channel/collect_manifests отдельно; здесь
        важны статусы, заголовки и тела.)
        """
        import http.client
        import ssl
        addr = addr or f"127.0.0.1:{self.port}"
        host, _, port = addr.partition(":")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        conn = http.client.HTTPSConnection(host, int(port), timeout=10,
                                           context=context)
        try:
            conn.request("GET", path, headers=headers or {})
            response = conn.getresponse()
            return response.status, response.headers, response.read()
        finally:
            conn.close()

    def seed_holder(self) -> dict:
        """Узел А: файл лежит, хеш известен, строка синкована."""
        folder = self.root / "files"
        folder.mkdir(exist_ok=True)
        path = folder / "Клип [vid000000001].mp4"
        path.write_bytes(b"ftyp-fake-payload-" + b"x" * 64)
        from app.indexer import file_hash
        digest = file_hash(path)
        vid, _ = repo.upsert_video(self.db_a.conn, {
            "platform": "youtube", "remote_id": "vid000000001",
            "key": "youtube:vid000000001", "title": "Клип",
            "webpage_url": "https://youtu.be/vid000000001",
            "origin": "yt-dlp"})
        repo.record_file(self.db_a.conn, vid, str(path), "video",
                         size=os.path.getsize(path), mtime=1.0,
                         digest=digest)
        conn = self.db_a.conn
        conn.execute(
            "UPDATE videos SET synced_at='2026-10-11T09:00:00' WHERE id=?",
            (vid,))
        self.server = server.PeerServer(self.db_a, port=0)
        self.port = self.server.start()
        return {"path": path, "digest": digest, "vid": vid}

    def seed_receiver(self) -> dict:
        """Узел Б: метаданные свои (синк есть), файла нет."""
        vid, _ = repo.upsert_video(self.db_b.conn, {
            "platform": "youtube", "remote_id": "vid000000001",
            "key": "youtube:vid000000001", "title": "Клип",
            "duration_s": 213,
            "webpage_url": "https://youtu.be/vid000000001",
            "origin": "yt-dlp"})
        self.db_b.conn.execute(
            "UPDATE videos SET synced_at='2026-10-11T09:00:00' WHERE id=?",
            (vid,))
        return {"vid": vid, "addr": f"127.0.0.1:{self.port}"}


class TestServer(MeshNetCase):
    def test_manifest_is_signed_and_truthful(self):
        holder = self.seed_holder()
        status, _headers, body = self.raw_get("/manifest/vid000000001")
        self.assertEqual(status, 200, "манифест должен отдаваться по TLS")
        item = json.loads(body)
        self.assertIsNone(manifest.verify(item), "манифест должен подписываться")
        self.assertEqual(item["sha256"], holder["digest"])
        self.assertEqual(item["synced_at"], "2026-10-11T09:00:00")

    def test_manifest_404_without_verified_hash(self):
        # Файл без хеша в индексе - отдаваться не должен (ничего непроверенного).
        vid, _ = repo.upsert_video(self.db_a.conn, {
            "platform": "youtube", "remote_id": "vid000000009",
            "key": "youtube:vid000000009", "title": "X", "origin": "yt-dlp"})
        path = self.root / "nohash.mp4"
        path.write_bytes(b"ftyp...")
        repo.record_file(self.db_a.conn, vid, str(path), "video",
                         size=7, mtime=1.0, digest=None)
        self.server = server.PeerServer(self.db_a, port=0)
        self.port = self.server.start()
        status, _headers, body = self.raw_get("/manifest/vid000000009")
        self.assertEqual(status, 404)
        self.assertIn("нет проверенного файла",
                      json.loads(body).get("error", ""))

    def test_file_with_and_without_range(self):
        holder = self.seed_holder()
        total = os.path.getsize(holder["path"])
        status, _headers, whole = self.raw_get("/file/vid000000001")
        self.assertEqual(status, 200)
        self.assertEqual(len(whole), total)

        status, headers, piece = self.raw_get("/file/vid000000001",
                                              headers={"Range": "bytes=5-9"})
        self.assertEqual(status, 206)
        self.assertEqual(headers.get("Content-Range"),
                         f"bytes 5-9/{total}")
        self.assertEqual(piece, whole[5:10])

    def test_unknown_paths_404(self):
        self.seed_holder()
        for path in ("/manifest/nope", "/file/nope", "/whatever"):
            status, _headers, _body = self.raw_get(path)
            self.assertEqual(status, 404, path)

    def test_plaintext_client_gets_nothing(self):
        """Старый клиент (http без TLS) не должен получить ни байта."""
        self.seed_holder()
        import socket
        with socket.create_connection(("127.0.0.1", self.port),
                                      timeout=5) as probe:
            probe.sendall(b"GET /manifest/vid000000001 HTTP/1.1\r\n"
                          b"Host: x\r\n\r\n")
            probe.settimeout(3)
            try:
                data = probe.recv(64)
            except OSError:
                data = b""
        self.assertTrue(not data or not data.startswith(b"HTTP/"),
                        f"plaintext не должен получать HTTP: {data[:20]!r}")
        # Сервер после такой попытки жив и отдаёт по TLS.
        status, _h, _b = self.raw_get("/manifest/vid000000001")
        self.assertEqual(status, 200)


class TestClient(MeshNetCase):
    def test_collect_skips_dead_peers(self):
        self.seed_holder()
        pairs = client.collect_manifests(
            "vid000000001", [self.seed_receiver()["addr"], "127.0.0.1:1"])
        self.assertEqual(len(pairs), 1, "мёртвый адрес просто выпадает")
        self.assertIsNone(manifest.verify(pairs[0][1]))

    def test_expect_mismatch_rejected_before_request(self):
        """Пин не совпал: запрос к серверу НЕ уходит (спайк: hits=0)."""
        self.seed_holder()
        addr = self.seed_receiver()["addr"]
        from app.mesh import server as server_mod
        hits: list = []
        original = server_mod._Handler.do_GET

        def counting(handler_self, *args, **kwargs):
            hits.append(handler_self.path)
            return original(handler_self, *args, **kwargs)

        with mock.patch.object(server_mod._Handler, "do_GET", counting):
            pairs = client.collect_manifests(
                "vid000000001", [addr], expects={addr: "bb" * 32})
        self.assertEqual(pairs, [], "чужой пин - пара не должна собраться")
        self.assertEqual(hits, [], "сервер не должен увидеть запрос вообще")

    def test_expect_match_returns_manifest_over_tls(self):
        self.seed_holder()
        addr = self.seed_receiver()["addr"]
        own_pub = identity.get_identity()[1].hex()
        pairs = client.collect_manifests(
            "vid000000001", [addr], expects={addr: own_pub})
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0][1]["pubkey"], own_pub,
                         "канал и манифест - один ключ")
        # Пин первого контакта запомнился в профиле.
        self.assertEqual(channel_mod.pin_get(addr), own_pub)

    def test_manifest_key_must_match_channel(self):
        """Манифест подписан не тем ключом, что открыл канал - отбрасывается."""
        self.seed_holder()
        addr = self.seed_receiver()["addr"]
        with mock.patch.object(channel_mod, "verify_peer",
                               return_value="ff" * 32):
            pairs = client.collect_manifests("vid000000001", [addr])
        self.assertEqual(pairs, [], "несовпадение ключа канала и подписи")

    def test_pick_digest_majority_and_filters(self):
        good = manifest.build("vid000000001", "sha256:" + "aa" * 32, 10)
        also = manifest.build("vid000000001", "sha256:" + "aa" * 32, 10)
        other = manifest.build("vid000000001", "sha256:" + "bb" * 32, 10)
        forged = {**good, "sha256": "sha256:" + "cc" * 32}  # подпись не сойдётся
        picked = client.pick_digest(
            [("a", good), ("b", also), ("c", other), ("d", forged)])
        self.assertEqual(picked, "sha256:" + "aa" * 32)
        # Забаненный мажоритарный хеш не выбирается.
        picked = client.pick_digest([("a", good), ("b", also)],
                                    banned={"sha256:" + "aa" * 32})
        self.assertIsNone(picked)

    def test_fetch_resumes_from_part(self):
        holder = self.seed_holder()
        part = self.root / "resume.part"
        whole = holder["path"].read_bytes()
        part.write_bytes(whole[:20])               # уже скачано 20 байт
        progress = []
        result = client.fetch("vid000000001", self.seed_receiver()["addr"],
                              str(part), progress=lambda done, total:
                              progress.append((done, total)))
        self.assertEqual(result["resumed_from"], 20)
        self.assertEqual(part.read_bytes(), whole, "дозапись должна собрать файл")

    def test_sniffer_ext(self):
        mp4 = self.root / "a.mp4"
        mp4.write_bytes(b"\x00\x00\x00\x18ftypisom....")
        self.assertEqual(client.sniffer_ext(mp4), "mp4")
        mkv = self.root / "a.mkv"
        mkv.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 32)
        self.assertEqual(client.sniffer_ext(mkv), "mkv")
        junk = self.root / "a.bin"
        junk.write_bytes(b"GARBAGE-NOT-MEDIA")
        self.assertEqual(client.sniffer_ext(junk), "mp4",
                         "неизвестное - безопасный дефолт")


class TestLimits(MeshNetCase):
    """B1.7: лимит размера - с обеих сторон (сервер не отдаёт, клиент не пишет)."""

    BIG = 11 * 1024 * 1024          # больше лимита по умолчанию (10 МиБ)
    LIMIT = 10 * 1024 * 1024

    def _big_holder(self) -> dict:
        """Холдер с файлом больше лимита (сервер смотрит размер на лету)."""
        holder = self.seed_holder()
        holder["path"].write_bytes(b"\x00" * self.BIG)
        return holder

    def test_server_refuses_oversized_file(self):
        """Сервер с лимитом: отказ 413, ни байта тела файла."""
        self._big_holder()
        limited = server.PeerServer(self.db_a, 0, max_bytes=self.LIMIT)
        port = limited.start()
        self.addCleanup(limited.stop)
        status, _headers, body = self.raw_get(
            "/file/vid000000001", addr=f"127.0.0.1:{port}")
        self.assertEqual(status, 413)
        error = json.loads(body).get("error", "")
        self.assertIn("лимит", error)
        self.assertIn("не отдаётся", error)

    def test_client_refuses_oversized_before_writing_part(self):
        """Клиентский лимит: отказ по Content-Length ДО создания part."""
        self._big_holder()
        receiver = self.seed_receiver()
        part = self.root / "limit" / "video.part"
        with self.assertRaises(RuntimeError) as caught:
            client.fetch("vid000000001", receiver["addr"], str(part),
                         max_bytes=self.LIMIT)
        self.assertIn("лимит", str(caught.exception))
        self.assertFalse(part.exists(),
                         "part не должен появиться при отказе по размеру")

    def test_encrypted_stream_limit_from_envelope(self):
        """Шифропуть: размер лежит в конверте - отказ до записи part."""
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.x25519 import \
            X25519PrivateKey
        self._big_holder()
        receiver = self.seed_receiver()
        part = self.root / "limit-enc" / "video.part"
        recipient = X25519PrivateKey.generate().public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        with self.assertRaises(RuntimeError) as caught:
            client.fetch("vid000000001", receiver["addr"], str(part),
                         encrypt_to=recipient, max_bytes=self.LIMIT)
        self.assertIn("лимит", str(caught.exception))
        self.assertFalse(part.exists(),
                         "шифропоток не должен начинаться сверх лимита")


class TestRestore(MeshNetCase):
    """Полный путь восстановления: от манифеста до скачанной строки."""

    def _settings(self, extra=None):
        settings = {"mesh_enabled": True,
                    "mesh_peers": f"127.0.0.1:{self.port}"}
        settings.update(extra or {})
        return settings

    def test_happy_path_records_file_and_provenance(self):
        holder = self.seed_holder()
        receiver = self.seed_receiver()
        row = {"id": receiver["vid"], "remote_id": "vid000000001",
               "title": "Клип"}
        dest = self.root / "out"
        with mock.patch.object(mesh_verify, "probe",
                               return_value={"checked": True, "ok": None,
                                             "duration": 213.0,
                                             "errors": [], "skipped": "тест"}):
            ok = mesh_fetch.restore(self.db_b.conn, row, self._settings(),
                                    str(dest), reason="удалено")
        self.assertTrue(ok)
        final = dest / "Клип [vid000000001].mp4"
        self.assertTrue(final.is_file(), "файл должен ложиться по шаблону с [id]")
        from app.indexer import file_hash
        self.assertEqual(file_hash(final), holder["digest"])

        files = [dict(r) for r in self.db_b.conn.execute(
            "SELECT path, kind, hash, format_json FROM files WHERE video_id=?",
            (receiver["vid"],))]
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["kind"], "video")
        provenance = json.loads(files[0]["format_json"])
        self.assertEqual(provenance["source"], "mesh")
        self.assertEqual(provenance["peer"], identity.get_identity()[1].hex())
        self.assertEqual(provenance["peer_synced_at"], "2026-10-11T09:00:00")
        status = self.db_b.conn.execute(
            "SELECT status FROM videos WHERE id=?",
            (receiver["vid"],)).fetchone()["status"]
        self.assertEqual(status, "downloaded")

    def test_tampered_bytes_rejected(self):
        self.seed_holder()
        # Файл подменили ПОСЛЕ того, как манифест подписал настоящий хеш.
        holder_path = self.root / "files" / "Клип [vid000000001].mp4"
        holder_path.write_bytes(b"totally-different-bytes")
        receiver = self.seed_receiver()
        row = {"id": receiver["vid"], "remote_id": "vid000000001",
               "title": "Клип"}
        dest = self.root / "out2"
        logs = []
        ok = mesh_fetch.restore(self.db_b.conn, row, self._settings(),
                                str(dest), log=logs.append, reason="")
        self.assertFalse(ok, "подменённые байты обязаны отклониться")
        self.assertTrue(any("хеш не сошёлся" in line for line in logs))
        self.assertFalse(list(dest.glob("*.mp4")),
                         "непроверенный файл не должен остаться")

    def test_banned_hash_never_fetched(self):
        holder = self.seed_holder()
        receiver = self.seed_receiver()
        row = {"id": receiver["vid"], "remote_id": "vid000000001",
               "title": "Клип"}
        from app.mesh import feedback
        feedback.ban(self.db_b.conn, holder["digest"], note="тест")
        logs = []
        ok = mesh_fetch.restore(self.db_b.conn, row, self._settings(),
                                str(self.root / "out3"), log=logs.append,
                                reason="")
        self.assertFalse(ok, "забаненный хеш не должен выбираться")
        self.assertTrue(any("стоп-листе" in line or
                            "пригодных" in line for line in logs), logs)

    def test_restore_tries_trusted_last_addr(self):
        """F5: последний рабочий адрес доверенного - в очереди адресатов.

        DHT мог не знать его (переезд, истёкший TTL): восстановление
        обязано пробовать проверенный путь и сразу ожидать по нему ключ
        устройства - личность подтверждается по TLS с первой пробы.
        """
        self.seed_holder()
        receiver = self.seed_receiver()
        row = {"id": receiver["vid"], "remote_id": "vid000000001",
               "title": "Клип"}
        key = "cd" * 32
        repo.truststore_add(self.db_b.conn, key, addr="198.51.100.7:4444")
        seen = {}

        def capture(remote_id, addrs, expects=None, **kw):
            seen["addrs"] = list(addrs)
            seen["expects"] = expects or {}
            return []

        with mock.patch.object(client, "collect_manifests",
                               side_effect=capture):
            ok = mesh_fetch.restore(self.db_b.conn, row, self._settings(),
                                    str(self.root / "out-f5"), reason="")
        self.assertFalse(ok, "без манифестов восстановление не пройдёт")
        self.assertIn("198.51.100.7:4444", seen["addrs"],
                      "адрес доверенного устройства обязан пробоваться")
        self.assertEqual(
            seen["addrs"][:2],
            [f"127.0.0.1:{self.port}", "198.51.100.7:4444"],
            "порядок: явные адреса участников, затем проверенный путь "
            "доверенного, и только потом DHT-перебор")
        self.assertEqual(seen["expects"].get("198.51.100.7:4444"), key,
                         "ожидание ключа = личность с первой пробы")

    def test_strict_blocks_unverified_holder(self):
        """1.2: strict принимает манифесты только от подтверждённых."""
        self.seed_holder()
        receiver = self.seed_receiver()
        row = {"id": receiver["vid"], "remote_id": "vid000000001",
               "title": "Клип"}
        settings = self._settings({"mesh_strict": True})
        logs = []
        ok = mesh_fetch.restore(self.db_b.conn, row, settings,
                                str(self.root / "out-strict"),
                                log=logs.append, reason="")
        self.assertFalse(ok, "неподтверждённый держатель в strict - отказ")
        self.assertTrue(any("strict" in line for line in logs), logs)
        self.assertFalse(list((self.root / "out-strict").glob("*.mp4"))
                         if (self.root / "out-strict").exists() else False)

        # подтверждаем ключ держателя - путь открывается
        pubkey = identity.get_identity()[1].hex()
        repo.truststore_verify(self.db_b.conn, pubkey)
        with mock.patch.object(mesh_verify, "probe",
                               return_value={"checked": True, "ok": None,
                                             "duration": 213.0,
                                             "errors": [], "skipped": "тест"}):
            ok = mesh_fetch.restore(self.db_b.conn, row, settings,
                                    str(self.root / "out-strict2"),
                                    log=logs.append, reason="")
        self.assertTrue(ok, logs)
    def test_friend_fetch_of_strangers_row(self):
        """2.1: чужая строка у друга - проверенный файл + провенанс.

        Правило B3 не ломается: строка в индекс НЕ заводится, хеш
        берётся из ПОДПИСАННОГО манифеста автора каталога (origin).
        """
        holder = self.seed_holder()
        receiver = self.seed_receiver()
        origin = identity.get_identity()[1].hex()
        dest = self.root / "friend-out"
        dest.mkdir()
        logs = []
        before = self.db_b.conn.execute(
            "SELECT COUNT(*) AS c FROM videos").fetchone()["c"]
        from app.mesh import verify as mesh_verify
        with mock.patch.object(mesh_verify, "probe",
                               return_value={"checked": True, "ok": None,
                                              "errors": []}):
            ok = mesh_fetch.restore_friend(
                self.db_b.conn,
                {"remote_id": "vid000000001", "origin": origin,
                 "title": "Клип"},
                receiver["addr"], str(dest), log=logs.append)
        self.assertTrue(ok, logs)
        files = list(dest.glob("*.mp4"))
        self.assertEqual(len(files), 1, files)
        from app.indexer import file_hash
        self.assertEqual(file_hash(files[0]), holder["digest"],
                         "sha256 сверен с подписанным манифестом")
        sidecar = files[0].with_suffix(".post.json")
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(payload["origin"], "friend")
        self.assertEqual(payload["from"], origin)
        self.assertEqual(payload["sha256"], holder["digest"])
        after = self.db_b.conn.execute(
            "SELECT COUNT(*) AS c FROM videos").fetchone()["c"]
        self.assertEqual(after, before, "строка в индекс НЕ заводится")

    def test_friend_fetch_refuses_foreign_key(self):
        """2.1: канал обязан открыться ключом автора каталога."""
        self.seed_holder()
        receiver = self.seed_receiver()
        dest = self.root / "friend-bad"
        dest.mkdir()
        logs = []
        ok = mesh_fetch.restore_friend(
            self.db_b.conn,
            {"remote_id": "vid000000001", "origin": "ab" * 32,
             "title": "Клип"},
            receiver["addr"], str(dest), log=logs.append)
        self.assertFalse(ok, "чужой ключ не должен проходить: " + str(logs))
        self.assertEqual(list(dest.iterdir()), [], "ничего не должно ляг")

    def test_redial_after_ip_change(self):
        """Критерий приёмки этапа 1: смена IP дружбы восстанавливается сама.

        mesh_peers пуст, DHT не поднят - единственный источник адресов -
        адресная книга (1.3): старый мёртвый адрес + свежий «новый IP».
        """
        self.seed_holder()
        receiver = self.seed_receiver()
        pubkey = identity.get_identity()[1].hex()
        repo.truststore_verify(self.db_b.conn, pubkey)
        repo.peer_addr_add(self.db_b.conn, pubkey, "127.0.0.1:1",
                           "probe")           # старый IP дружбы (мёртв)
        repo.peer_addr_add(self.db_b.conn, pubkey, receiver["addr"],
                           "fetch")           # «новый IP» после переезда
        row = {"id": receiver["vid"], "remote_id": "vid000000001",
               "title": "Клип"}
        settings = {"mesh_enabled": True, "mesh_strict": True}
        logs = []
        dest = self.root / "out-redial"
        with mock.patch.object(mesh_verify, "probe",
                               return_value={"checked": True, "ok": None,
                                             "duration": 213.0,
                                             "errors": [], "skipped": "тест"}):
            ok = mesh_fetch.restore(self.db_b.conn, row, settings,
                                    str(dest), log=logs.append, reason="")
        self.assertTrue(ok, f"редиал по адресной книге должен найти "
                            f"друга после смены IP: {logs}")
        self.assertTrue((dest / "Клип [vid000000001].mp4").is_file())
        # успешный перенос обновил свежесть адреса (источник fetch)
        fresh = repo.peer_addr_list(self.db_b.conn, pubkey, limit=1)
        self.assertEqual(fresh[0]["addr"], receiver["addr"])

    def test_revoked_device_is_refused_and_restorable(self):
        """Отзыв доверия (truststore) отсекает манифесты и будущие файлы.

        Ключ держателя известен (в тестах оба узла в одном процессе) -
        отзываем его у восстанавливающего и проверяем, что путь закрыт,
        а снятие отзыва открывает его снова.
        """
        self.seed_holder()
        receiver = self.seed_receiver()
        pubkey = identity.get_identity()[1].hex()
        repo.truststore_revoke(self.db_b.conn, pubkey)
        row = {"id": receiver["vid"], "remote_id": "vid000000001",
               "title": "Клип"}
        dest = self.root / "out-revoked"
        logs = []
        ok = mesh_fetch.restore(self.db_b.conn, row, self._settings(),
                                str(dest), log=logs.append, reason="")
        self.assertFalse(ok, "отзыв обязан отсекать и будущие файлы")
        self.assertTrue(any("отозванным доверием" in line for line in logs),
                        logs)
        self.assertTrue(any("нет пригодных манифестов" in line
                            for line in logs), logs)
        self.assertFalse(list(dest.glob("*.mp4")))

        # Снятие отзыва возвращает путь восстановления
        repo.truststore_restore(self.db_b.conn, pubkey)
        with mock.patch.object(mesh_verify, "probe",
                               return_value={"checked": True, "ok": None,
                                             "duration": 213.0,
                                             "errors": [], "skipped": "тест"}):
            ok = mesh_fetch.restore(self.db_b.conn, row, self._settings(),
                                    str(dest), reason="")
        self.assertTrue(ok, "после снятия отзыва файл должен дойти")

    def test_missing_own_metadata_refused(self):
        self.seed_holder()
        row = {"id": 777, "remote_id": "vid000000001", "title": "Без меты"}
        self.db_b.conn.execute(
            "INSERT INTO videos (key, platform, remote_id, title, status, "
            "first_seen_at, updated_at) VALUES "
            "('youtube:vid000000002','youtube','vid000000002','X','known',"
            "'t','t')")
        row["id"] = self.db_b.conn.execute(
            "SELECT id FROM videos WHERE remote_id='vid000000002'"
        ).fetchone()["id"]
        logs = []
        ok = mesh_fetch.restore(self.db_b.conn, row, self._settings(),
                                str(self.root / "out4"), log=logs.append)
        self.assertFalse(ok, "без своих метаданных сверять нечего")
        self.assertTrue(any("нет своих метаданных" in line for line in logs))

    def test_peer_reputation_is_informational(self):
        """Носитель с историей: приём проходит, пометка - обязательна.

        Репутация ключа (сколько раз он приносил «несовпадение») - это
        информация для человека в журнале и в провенансе, а не барьер:
        блокирует только стоп-лист по самому хешу.
        """
        self.seed_holder()
        receiver = self.seed_receiver()
        from app.mesh import feedback
        pubkey = identity.get_identity()[1].hex()
        feedback.ban(self.db_b.conn, "sha256:" + "77" * 32,
                     peer=pubkey, note="старое несовпадение")
        feedback.ban(self.db_b.conn, "sha256:" + "78" * 32,
                     peer=pubkey, note="ещё одно")
        row = {"id": receiver["vid"], "remote_id": "vid000000001",
               "title": "Клип"}
        logs = []
        with mock.patch.object(mesh_verify, "probe",
                               return_value={"checked": True, "ok": None,
                                             "duration": 213.0,
                                             "errors": [], "skipped": "тест"}):
            ok = mesh_fetch.restore(self.db_b.conn, row, self._settings(),
                                    str(self.root / "out7"),
                                    log=logs.append, reason="")
        self.assertTrue(ok, "репутация - информация, не барьер")
        self.assertTrue(any("несовпадения" in line for line in logs),
                        f"в журнале должна быть пометка: {logs}")
        files = [dict(r) for r in self.db_b.conn.execute(
            "SELECT format_json FROM files WHERE video_id=?",
            (receiver["vid"],))]
        provenance = json.loads(files[0]["format_json"])
        self.assertEqual(provenance["peer_offenses"], 2,
                         "провенанс обязан хранить историю носителя")

    def test_structure_failure_rejected(self):
        self.seed_holder()
        receiver = self.seed_receiver()
        row = {"id": receiver["vid"], "remote_id": "vid000000001",
               "title": "Клип"}
        with mock.patch.object(mesh_verify, "probe",
                               return_value={"checked": True, "ok": False,
                                             "duration": 5.0,
                                             "errors": ["длительность"]}):
            logs = []
            ok = mesh_fetch.restore(self.db_b.conn, row, self._settings(),
                                    str(self.root / "out5"),
                                    log=logs.append)
        self.assertFalse(ok)
        self.assertTrue(any("структурная проверка" in line for line in logs))

    def test_dht_lookup_supplies_addresses(self):
        """restore БЕЗ ручных адресов находит участника через DHT.

        Сквозной путь: узел-анонсер объявляет файл (TCP-сервер А) ->
        значение лежит у узел-слушателя -> restore берёт адрес из DHT ->
        манифесты -> перенос -> проверки -> скачанный файл.
        """
        from app.mesh import dht as dht_mod
        holder = self.seed_holder()
        receiver = self.seed_receiver()
        row = {"id": receiver["vid"], "remote_id": "vid000000001",
               "title": "Клип"}
        # own в конструкторе: подмена после создания оставила бы в
        # RoutingTable устаревший id (самопроверка перестала бы ловить).
        announcer = dht_mod.DHTNode(multicast=False, port=0,
                                    own=os.urandom(dht_mod.NODE_ID_LEN))
        listener = dht_mod.DHTNode(multicast=False, port=0,
                                   own=os.urandom(dht_mod.NODE_ID_LEN))
        with mock.patch.object(dht_mod, "BOOTSTRAP", ()):
            pa = announcer.start_sync()
            pl = listener.start_sync()
            try:
                announcer.table.add(dht_mod.Contact(listener.own, "127.0.0.1", pl))
                listener.table.add(dht_mod.Contact(announcer.own, "127.0.0.1", pa))
                accepted = announcer.announce_sync("vid000000001", self.port)
                self.assertGreater(accepted, 0, "значение должно дойти")
                dht_mod.set_current(listener)
                settings = {"mesh_enabled": True}    # БЕЗ ручных адресов!
                logs = []
                with mock.patch.object(mesh_verify, "probe",
                                       return_value={"checked": True,
                                                     "ok": None,
                                                     "duration": 213.0,
                                                     "errors": []}):
                    ok = mesh_fetch.restore(self.db_b.conn, row, settings,
                                            str(self.root / "out6"),
                                            log=logs.append, reason="")
                self.assertTrue(ok, logs)
                self.assertTrue((self.root / "out6" /
                                 "Клип [vid000000001].mp4").is_file())
            finally:
                dht_mod.set_current(None)
                announcer.stop_sync()
                listener.stop_sync()


class TestCatalog(MeshNetCase):
    """B3: каталог держателя - подпись, канал, зеркало, восстановление."""

    def test_catalog_endpoint_signed_and_truthful(self):
        holder = self.seed_holder()
        status, _headers, body = self.raw_get("/catalog")
        self.assertEqual(status, 200)
        snapshot = json.loads(body)
        self.assertEqual(snapshot["pubkey"],
                         identity.get_identity()[1].hex())
        entries, error = catalog_mod.verify(snapshot)
        self.assertIsNone(error, error)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["remote_id"], "vid000000001")
        self.assertEqual(entries[0]["sha256"], holder["digest"])
        self.assertTrue(entries[0]["title"])

    def test_tampered_catalog_refused_by_client(self):
        """Подмена тела после подписи ловится проверкой каталога."""
        self.seed_holder()
        status, _headers, body = self.raw_get("/catalog")
        snapshot = json.loads(body)
        snapshot["entries"][0]["sha256"] = "sha256:" + "ff" * 32
        entries, error = catalog_mod.verify(snapshot)
        self.assertIsNone(entries)
        self.assertIn("подпись", error)
        self.assertEqual(status, 200, "сам сервер отвечает честно")

    def test_collect_and_apply_lww_snapshot(self):
        self.seed_holder()
        receiver = self.seed_receiver()
        found = client.collect_catalogs([receiver["addr"]])
        self.assertEqual(len(found), 1)
        addr, pubkey, entries = found[0]
        self.assertEqual(pubkey, identity.get_identity()[1].hex())

        stats = repo.catalog_apply(self.db_b.conn, pubkey, entries,
                                   addr=addr)
        self.assertEqual(stats["added"], 1)
        rows = repo.catalog_list(self.db_b.conn)
        self.assertEqual(rows[0]["remote_id"], "vid000000001")
        self.assertEqual(rows[0]["own"], 1, "строка есть и в моём индексе")

        stats = repo.catalog_apply(self.db_b.conn, pubkey, entries,
                                   addr=addr)
        self.assertEqual((stats["added"], stats["updated"]), (0, 0),
                         "повторный снапшот без изменений - тихо")

        stats = repo.catalog_apply(self.db_b.conn, pubkey, [], addr=addr)
        self.assertEqual(stats["removed"], 1,
                         "снимок без строки = файл у участника ушёл")
        self.assertEqual(repo.catalog_list(self.db_b.conn), [])

    def test_restore_from_catalog_with_pinned_key(self):
        """Зеркало -> своя строка -> восстановление с expect=origin."""
        self.seed_holder()
        receiver = self.seed_receiver()
        found = client.collect_catalogs([receiver["addr"]])
        addr, pubkey, entries = found[0]
        repo.catalog_apply(self.db_b.conn, pubkey, entries, addr=addr)

        own = repo.catalog_own(self.db_b.conn, "vid000000001")
        self.assertIsNotNone(own)
        row = {"id": own["id"], "remote_id": "vid000000001",
               "title": own["title"]}
        # Канал обязан открыться ключом автора каталога
        settings = {"mesh_enabled": True,
                    "mesh_peers": f"{addr}@{pubkey}"}
        dest = self.root / "out-catalog"
        with mock.patch.object(mesh_verify, "probe",
                               return_value={"checked": True, "ok": None,
                                             "duration": 213.0,
                                             "errors": [], "skipped": "тест"}):
            ok = mesh_fetch.restore(self.db_b.conn, row, settings,
                                    str(dest), reason="каталог")
        self.assertTrue(ok, "восстановление из зеркала должно пройти")
        self.assertTrue((dest / "Клип [vid000000001].mp4").is_file())


class TestEncryptedFetch(MeshNetCase):
    """B2: файл уходит шифроблоками под ключ получателя."""

    def test_encrypted_fetch_roundtrip(self):
        holder = self.seed_holder()
        self.seed_receiver()
        part = self.root / "enc.part"
        result = client.fetch(
            "vid000000001", f"127.0.0.1:{self.port}", str(part),
            encrypt_to=crypt_mod.own_x25519_public())
        self.assertEqual(result["resumed_from"], 0,
                         "шифрованный путь начинается с нуля")
        self.assertEqual(result["written"], os.path.getsize(holder["path"]))
        from app.indexer import file_hash
        self.assertEqual(file_hash(str(part)), holder["digest"],
                         "расшифрованный файл должен сходиться по хешу")

    def test_range_with_encryption_refused(self):
        self.seed_holder()
        key = crypt_mod.own_x25519_public().hex()
        status, _headers, body = self.raw_get(
            f"/file/vid000000001?enc=v1&pk={key}",
            headers={"Range": "bytes=10-"})
        self.assertEqual(status, 400, "дозапись шифропотока не поддержана")
        self.assertIn("дозапись", json.loads(body).get("error", ""))

    def test_plaintext_still_works_beside_encryption(self):
        """Старый клиент (без ?enc) не замечен - обратная совместимость."""
        holder = self.seed_holder()
        status, _headers, body = self.raw_get("/file/vid000000001")
        self.assertEqual(status, 200)
        from app.indexer import file_hash
        part = self.root / "plain.bin"
        part.write_bytes(body)
        self.assertEqual(file_hash(str(part)), holder["digest"])

    def test_restore_with_mesh_encrypt_setting(self):
        self.seed_holder()
        receiver = self.seed_receiver()
        row = {"id": receiver["vid"], "remote_id": "vid000000001",
               "title": "Клип"}
        settings = {"mesh_enabled": True,
                    "mesh_peers": f"127.0.0.1:{self.port}",
                    "mesh_encrypt": True}
        logs = []
        dest = self.root / "out-enc"
        with mock.patch.object(mesh_verify, "probe",
                               return_value={"checked": True, "ok": None,
                                             "duration": 213.0,
                                             "errors": [], "skipped": "тест"}):
            ok = mesh_fetch.restore(self.db_b.conn, row, settings,
                                    str(dest), log=logs.append, reason="")
        self.assertTrue(ok, logs)
        self.assertTrue(any("под мой ключ" in line for line in logs), logs)
        self.assertTrue((dest / "Клип [vid000000001].mp4").is_file())


if __name__ == "__main__":
    unittest.main()
