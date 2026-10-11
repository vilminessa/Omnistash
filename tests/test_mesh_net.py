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
from app.mesh import client, fetch as mesh_fetch, identity, manifest, server
from app.mesh import verify as mesh_verify


class MeshNetCase(unittest.TestCase):
    """Два узла: А (владеет файлом) и Б (восстанавливает)."""

    def setUp(self):
        # Идентичность - свежая на тестовый профиль (подписи манифестов).
        self._prof = tempfile.TemporaryDirectory()
        self.addCleanup(self._prof.cleanup)
        patcher = mock.patch.object(identity, "profile_dir",
                                    lambda: Path(self._prof.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        identity.reset_cache()
        self.addCleanup(identity.reset_cache)

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
        import urllib.request
        url = f"http://127.0.0.1:{self.port}/manifest/vid000000001"
        with urllib.request.urlopen(url, timeout=10) as response:
            item = json.loads(response.read())
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
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/manifest/vid000000009",
                timeout=10)
        self.assertEqual(caught.exception.code, 404)

    def test_file_with_and_without_range(self):
        holder = self.seed_holder()
        import urllib.request
        base = f"http://127.0.0.1:{self.port}/file/vid000000001"
        with urllib.request.urlopen(base, timeout=10) as response:
            whole = response.read()
        self.assertEqual(len(whole), os.path.getsize(holder["path"]))

        request = urllib.request.Request(base, headers={"Range": "bytes=5-9"})
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.headers.get("Content-Range"),
                             f"bytes 5-9/{os.path.getsize(holder['path'])}")
            piece = response.read()
        self.assertEqual(piece, whole[5:10])

    def test_unknown_paths_404(self):
        self.seed_holder()
        import urllib.error
        for path in ("/manifest/nope", "/file/nope", "/whatever"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}{path}", timeout=10)
            self.assertEqual(caught.exception.code, 404, path)


class TestClient(MeshNetCase):
    def test_collect_skips_dead_peers(self):
        self.seed_holder()
        pairs = client.collect_manifests(
            "vid000000001", [self.seed_receiver()["addr"], "127.0.0.1:1"])
        self.assertEqual(len(pairs), 1, "мёртвый адрес просто выпадает")
        self.assertIsNone(manifest.verify(pairs[0][1]))

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


if __name__ == "__main__":
    unittest.main()
