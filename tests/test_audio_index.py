"""Аудио (E0): скан аудио-файлов, метки файла, тип строки, фильтры и очередь.

Честность меток проверяем подменой ffprobe: реального ffmpeg в тестах нет,
а значения «как из ffprobe» и «не получена» собираются из тех же веток кода,
что и в живой пробе. Сеть тесты не трогает: классификация аудио-площадок -
только разбор URL, без fetch.
"""

import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app import audio as audio_mod
from app import repo
from app.db import Database
from app.gui import Api
from app.indexer import scan
from app.metadata import (audio_host_of, classify_url, flat_entry,
                          normalize_video, video_url)
from app.util import now_iso


def sync_safe(size: int) -> bytes:
    """ID3-размер: 4 байта по 7 значимых бит, старший - в первом байте."""
    return bytes(((size >> (21 - 7 * index)) & 0x7F) for index in range(4))


def text_frame(text: str) -> bytes:
    """Текстовый кадр ID3: encoding 3 (UTF-8) + строка."""
    return b"\x03" + text.encode("utf-8")


def apic_frame(blob: bytes) -> bytes:
    """APIC: latin-1 mime, пустое описание, сами байты картинки."""
    return b"\x00" + b"image/jpeg" + b"\x00" + b"" + b"\x00" + blob


def id3v2(frames: list[tuple[str, bytes]]) -> bytes:
    """Собрать ID3v2.3: заголовок + кадры (кадр = 4 байта ключа)."""
    body = b""
    for key, payload in frames:
        body += key.encode("latin-1")
        body += len(payload).to_bytes(4, "big") + b"\x00\x00" + payload
    return b"ID3" + b"\x03\x00\x00" + sync_safe(len(body)) + body


def fake_probe_run(duration="254.6", bit_rate="320000"):
    """Подмена запуска ffprobe: тот же json, что просит probe()."""
    def run(cmd, timeout=15.0):
        payload = {
            "format": {"duration": duration, "bit_rate": bit_rate},
            "streams": [{"codec_name": "mp3", "codec_type": "audio",
                         "sample_rate": "44100", "channels": 2}],
        }
        return subprocess.CompletedProcess(cmd, 0,
                                          json.dumps(payload).encode("utf-8"), b"")
    return run


class AudioScanCase(unittest.TestCase):
    """Скан с аудио: кэш обложек (profile_dir) подменён на временный."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.root = self.dir / "library"
        self.root.mkdir(parents=True)
        self.db = Database(self.dir / "library.db")
        self.conn = self.db.conn
        self.roots = [{"path": str(self.root), "recursive": True, "enabled": True}]
        covers = self.dir / "covers"
        covers.mkdir()
        patcher = mock.patch("app.audio.cover_dir", return_value=covers)
        patcher.start()
        # Cleanup LIFO: патч обложек -> закрытие БД -> удаление папки.
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.db.close)
        self.addCleanup(patcher.stop)

    def write(self, name, payload: bytes):
        path = self.root / name
        path.write_bytes(payload)
        return path

    def run_scan(self, **kwargs):
        kwargs.setdefault("compute_hash", True)
        return scan(self.roots, self.db, **kwargs)

    def videos(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT id, title, status, media_kind, duration_s FROM videos ORDER BY id")]

    def video(self, vid):
        return dict(self.conn.execute(
            "SELECT id, title, status, media_kind, duration_s, platform "
            "FROM videos WHERE id=?", (vid,)).fetchone())

    def files(self, vid):
        return [dict(r) for r in self.conn.execute(
            "SELECT kind, path, format_json FROM files WHERE video_id=?", (vid,))]

    def audio_format(self, vid):
        """Метки аудио из строки files(kind='video') - того же файла."""
        row = self.conn.execute(
            "SELECT format_json FROM files WHERE video_id=? AND kind='video'",
            (vid,)).fetchone()
        return json.loads(row["format_json"])

    def mp3_with_tags(self) -> bytes:
        """mp3 с ID3v2: название, исполнитель, альбом, обложка + «дорожка»."""
        cover = b"\xff\xd8\xff" + b"jpeg-contents" * 30
        frames = [("TIT2", text_frame("Джазовый мик")),
                  ("TPE1", text_frame("Квинтет")),
                  ("TALB", text_frame("Сессия 2024")),
                  ("APIC", apic_frame(cover))]
        return id3v2(frames) + b"fake-mpeg-stream"


class TestAudioScan(AudioScanCase):
    def test_audio_files_become_audio_rows(self):
        self.write("Джазовый мик.mp3", self.mp3_with_tags())
        self.write("Неизвестный трек.m4a", b"broken-m4a-body")
        self.write("Голоса.opus", b"raw-opus-body")
        with mock.patch("app.audio.find_ffprobe", return_value=None):
            report = self.run_scan()
        self.assertEqual(report["added"], 3)
        self.assertEqual(report["audio"], 3)
        self.assertEqual({row["media_kind"] for row in self.videos()}, {"audio"})
        # Файл лежит той же строкой kind='video': `kind` - роль файла в
        # записи, а тип дорожки - на самой строке (videos.media_kind).
        for row in self.videos():
            kinds = {f["kind"] for f in self.files(row["id"])}
            self.assertIn("video", kinds)
        # Метка в строке files: media='audio' - ключ для карточки окна.
        mp3 = next(row for row in self.videos() if row["title"] == "Джазовый мик")
        meta = self.audio_format(mp3["id"])
        self.assertEqual(meta["media"], "audio")
        self.assertEqual(meta["format"], ".mp3")
        self.assertIsNone(meta["probe"])

    def test_probe_labels_written(self):
        self.write("Трек.mp3", self.mp3_with_tags())
        with mock.patch("app.audio.find_ffprobe",
                        return_value=str(self.dir / "ffprobe.exe")), \
             mock.patch("app.audio._run", side_effect=fake_probe_run()):
            report = self.run_scan()
        self.assertEqual(report["audio"], 1)
        row = self.videos()[0]
        self.assertEqual(row["media_kind"], "audio")
        self.assertIsNotNone(row["duration_s"])
        meta = self.audio_format(row["id"])
        self.assertEqual(meta["probe"], "ffprobe")
        self.assertAlmostEqual(meta["duration_s"], 254.6)
        self.assertEqual(meta["bitrate_bps"], 320000)
        self.assertEqual(meta["sample_rate_hz"], 44100)
        self.assertEqual(meta["channels"], 2)
        self.assertEqual(meta["codec"], "mp3")

    def test_tags_and_cover_from_file(self):
        self.write("Джазовый мик.mp3", self.mp3_with_tags())
        with mock.patch("app.audio.find_ffprobe", return_value=None):
            self.run_scan()
        row = self.videos()[0]
        meta = self.audio_format(row["id"])
        # Метки из тега самого файла + схема прочитана (честная, не гадалка).
        self.assertEqual(meta["title"], "Джазовый мик")
        self.assertEqual(meta["artist"], "Квинтет")
        self.assertEqual(meta["album"], "Сессия 2024")
        self.assertEqual(meta["tags_scheme"], "id3v2")
        # Обложка из тега -> файл кэша профиля и строка kind='thumbnail'.
        self.assertTrue(Path(meta["cover_path"]).exists())
        cover_row = next(f for f in self.files(row["id"]) if f["kind"] == "thumbnail")
        self.assertEqual(cover_row["path"], meta["cover_path"])

    def test_without_ffprobe_labels_are_honest(self):
        self.write("Трек.mp3", b"ID3" + b"\x03\x00\x00" + sync_safe(0) + b"no-tags")
        with mock.patch("app.audio.find_ffprobe", return_value=None):
            self.run_scan()
        row = self.videos()[0]
        self.assertIsNone(row["duration_s"])
        meta = self.audio_format(row["id"])
        self.assertIsNone(meta["probe"])
        self.assertIn("не получены", meta["probe_note"])
        self.assertIn("ffprobe", meta["probe_note"])

    def test_ffprobe_cannot_read_is_reported(self):
        self.write("Битый.mp3", b"mp3-but-not-probed")
        broken = lambda cmd, timeout=15.0: subprocess.CompletedProcess(
            cmd, 1, b"", b"ffprobe error")
        with mock.patch("app.audio.find_ffprobe",
                        return_value=str(self.dir / "ffprobe.exe")), \
             mock.patch("app.audio._run", side_effect=broken):
            self.run_scan()
        row = self.videos()[0]
        meta = self.audio_format(row["id"])
        self.assertIsNone(meta["probe"])
        self.assertIn("не прочитал файл", meta["probe_note"])
        self.assertIsNone(row["duration_s"])

    def test_rescan_does_not_read_probe_again(self):
        self.write("Трек.mp3", self.mp3_with_tags())
        with mock.patch("app.audio.find_ffprobe", return_value=None):
            first = self.run_scan()
            again = self.run_scan()
        self.assertEqual(first["audio"], 1)
        self.assertEqual(again["unchanged"], 1)
        self.assertEqual(again["audio"], 0)  # метки уже лежат в строке files

    def test_video_row_keeps_kind_and_platform_duration(self):
        # Аудио-файл, связанный с существующей ВИДЕО-строкой: строка
        # остаётся видео и со своей длительностью площадки.
        vid, _ = repo.upsert_video(self.conn, normalize_video(
            {"id": "abc000000001", "title": "Джазовый мик"}), full=True)
        self.conn.execute("UPDATE videos SET duration_s=400 WHERE id=?", (vid,))
        self.write("Джазовый мик.mp3", self.mp3_with_tags())
        with mock.patch("app.audio.find_ffprobe",
                        return_value=str(self.dir / "ffprobe.exe")), \
             mock.patch("app.audio._run", side_effect=fake_probe_run()):
            report = self.run_scan()
        self.assertEqual(report["bound_title"], 1)
        row = self.video(vid)
        self.assertEqual(row["media_kind"], "video")
        self.assertEqual(row["duration_s"], 400)


class TestAudioMetadata(unittest.TestCase):
    def test_classify_soundcloud_track_and_channel(self):
        track = classify_url("https://soundcloud.com/author/my-track")
        self.assertEqual(track["platform"], "soundcloud")
        self.assertEqual(track["media_kind"], "audio")
        self.assertEqual(track["kind"], "video")
        self.assertEqual(track["video_id"], "my-track")
        self.assertEqual(track["handle"], "author")

        profile = classify_url("https://soundcloud.com/author")
        self.assertEqual(profile["kind"], "channel")
        self.assertEqual(profile["handle"], "author")
        self.assertEqual(profile["media_kind"], "audio")

        discover = classify_url("https://soundcloud.com/discover/top")
        self.assertEqual(discover["kind"], "unknown")
        self.assertEqual(discover["media_kind"], "audio")

    def test_classify_bandcamp_album_and_track(self):
        album = classify_url("https://artist.bandcamp.com/album/release-name")
        self.assertEqual(album["platform"], "bandcamp")
        self.assertEqual(album["media_kind"], "audio")
        self.assertEqual(album["kind"], "playlist")
        self.assertEqual(album["playlist_id"], "release-name")

        track = classify_url("https://artist.bandcamp.com/album/release-name/track-two")
        self.assertEqual(track["kind"], "video")
        self.assertEqual(track["playlist_id"], "release-name")
        self.assertEqual(track["video_id"], "track-two")

        channel = classify_url("https://artist.bandcamp.com/")
        self.assertEqual(channel["kind"], "channel")
        self.assertEqual(channel["handle"], "artist")
        self.assertEqual(channel["media_kind"], "audio")

    def test_audio_host_of_subdomains(self):
        self.assertEqual(audio_host_of("https://soundcloud.com/x"), "soundcloud")
        self.assertEqual(audio_host_of("https://m.soundcloud.com/x/y"), "soundcloud")
        self.assertEqual(audio_host_of("https://artist.bandcamp.com/a"), "bandcamp")
        self.assertEqual(audio_host_of("https://www.bandcamp.com/a"), "bandcamp")
        self.assertIsNone(audio_host_of("https://youtube.com/watch?v=x"))

    def test_normalize_video_audio_hint(self):
        audio = normalize_video({"id": "t1", "title": "Трек", "type": "audio"})
        self.assertEqual(audio["media_kind"], "audio")
        by_extractor = normalize_video({"id": "t2", "title": "Т",
                                        "extractor": "soundcloud"})
        self.assertEqual(by_extractor["media_kind"], "audio")
        audio_only = normalize_video({"id": "t3", "title": "Т",
                                      "acodec": "mp3", "vcodec": None})
        self.assertEqual(audio_only["media_kind"], "audio")
        # vcodec вместе с acodec - видеопоток: «аудио» здесь нет.
        video = normalize_video({"id": "t4", "title": "Т",
                                 "acodec": "mp4", "vcodec": "vp9"})
        self.assertIsNone(video["media_kind"])
        # Ни кодека, ни audio-признака: «не известно», а не «аудио».
        self.assertIsNone(normalize_video({"id": "t5", "title": "Т"})["media_kind"])

    def test_flat_entry_audio_hint(self):
        entry = flat_entry({"id": "t1", "title": "Т", "vcodec": None,
                            "acodec": "mp3"}, 1)
        self.assertEqual(entry["media_kind"], "audio")
        video = flat_entry({"id": "t2", "title": "Т", "vcodec": "vp9",
                            "acodec": "mp4"}, 2)
        self.assertIsNone(video["media_kind"])

    def test_video_url_none_for_audio_platforms(self):
        # Гадать youtube-шаблон для чужой площадки не будем.
        self.assertIsNone(video_url("soundcloud", "my-track"))
        self.assertIsNone(video_url("bandcamp", "release-name"))

    def test_upsert_video_keeps_audio_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = Database(Path(tmp) / "audio.db").conn
            vid, created = repo.upsert_video(conn, {
                "platform": "soundcloud", "remote_id": "t1",
                "key": "soundcloud:t1", "title": "Трек", "raw_json": "{}",
                "origin": "yt-dlp", "media_kind": "audio"})
            self.assertTrue(created)
            self.assertEqual(conn.execute(
                "SELECT media_kind FROM videos WHERE id=?", (vid,)).fetchone()["media_kind"],
                "audio")
            # Быстрый синк без media_kind (coalesce=None) - аудио остаётся.
            repo.upsert_video(conn, {"platform": "soundcloud",
                                     "remote_id": "t1", "title": "Новое имя"})
            row = conn.execute("SELECT media_kind, title FROM videos WHERE id=?",
                               (vid,)).fetchone()
            self.assertEqual(row["media_kind"], "audio")
            self.assertEqual(row["title"], "Новое имя")
            conn.close()


class TestMediaKindRepo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "index.db")
        self.conn = self.db.conn
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.db.close)

    def add_row(self, remote_id, title, *, kind="video", status="known"):
        vid, _ = repo.upsert_video(self.conn, {
            "platform": "youtube", "remote_id": remote_id,
            "key": f"youtube:{remote_id}", "title": title,
            "raw_json": "{}", "origin": "yt-dlp"})
        if kind != "video":
            repo.set_media_kind(self.conn, vid, kind)
        if status != "known":
            self.conn.execute("UPDATE videos SET status=? WHERE id=?", (status, vid))
        return vid

    def test_migration_default_and_idempotent(self):
        # Старая строка после миграции v12 получает честный дефолт 'video'.
        vid = self.add_row("vid00000001", "Клип")
        self.assertEqual(self.conn.execute(
            "SELECT media_kind FROM videos WHERE id=?", (vid,)).fetchone()["media_kind"],
            "video")
        self.db.migrate()   # повтор миграции не роняет и не меняет данные
        self.db.migrate()
        row = dict(self.conn.execute(
            "SELECT media_kind, title FROM videos WHERE id=?", (vid,)).fetchone())
        self.assertEqual(row["media_kind"], "video")
        self.assertEqual(row["title"], "Клип")

    def test_set_media_kind_validation_and_updated_at(self):
        vid = self.add_row("vid00000002", "Клип")
        before = self.conn.execute(
            "SELECT updated_at FROM videos WHERE id=?", (vid,)).fetchone()["updated_at"]
        self.assertEqual(repo.set_media_kind(self.conn, vid, "audio"), 1)
        row = self.conn.execute(
            "SELECT media_kind, updated_at FROM videos WHERE id=?", (vid,)).fetchone()
        self.assertEqual(row["media_kind"], "audio")
        # Смена типа не двигает updated_at: очередь сортируется по нему.
        self.assertEqual(row["updated_at"], before)
        with self.assertRaises(ValueError):
            repo.set_media_kind(self.conn, vid, "wat")

    def test_list_filters_priority_and_label(self):
        vid_video = self.add_row("vid00000003", "Клип")
        vid_audio = self.add_row("vid00000004", "Трек", kind="audio")
        common = repo.list_videos(self.conn)
        self.assertEqual(common["total"], 2)
        labels = {row["id"]: row["media_label"] for row in common["rows"]}
        self.assertEqual(labels[vid_audio], "аудио")
        self.assertEqual(labels[vid_video], "видео")

        audio_only = repo.list_videos(self.conn, media_kind="audio")
        self.assertEqual([row["id"] for row in audio_only["rows"]], [vid_audio])

        # Явный фильтр окна важнее настройки: exclude_audio с media_kind
        # не работает.
        only_audio = repo.list_videos(self.conn, media_kind="audio",
                                      exclude_audio=True)
        self.assertEqual([row["id"] for row in only_audio["rows"]], [vid_audio])
        only_video = repo.list_videos(self.conn, media_kind="video",
                                      exclude_audio=True)
        self.assertEqual([row["id"] for row in only_video["rows"]], [vid_video])

        hidden = repo.list_videos(self.conn, exclude_audio=True)
        self.assertEqual([row["id"] for row in hidden["rows"]], [vid_video])

    def test_list_sort_by_media_kind(self):
        self.add_row("vid00000005", "Клип")
        self.add_row("vid00000006", "Трек", kind="audio")
        asc = repo.list_videos(self.conn, order={"col": "media_kind", "dir": "asc"})
        self.assertEqual([row["media_kind"] for row in asc["rows"]],
                         ["audio", "video"])
        desc = repo.list_videos(self.conn, order={"col": "media_kind", "dir": "desc"})
        self.assertEqual([row["media_kind"] for row in desc["rows"]],
                         ["video", "audio"])

    def test_list_unknown_media_kind_ignored(self):
        self.add_row("vid00000007", "Клип")
        self.add_row("vid00000008", "Трек", kind="audio")
        result = repo.list_videos(self.conn, media_kind="wat")
        self.assertEqual(result["total"], 2)  # белый список: прочее молча

    def test_enqueue_skips_audio_by_default(self):
        vid_video = self.add_row("vid00000009", "Клип")
        vid_audio = self.add_row("vid00000010", "Трек", kind="audio")
        self.assertEqual(repo.enqueue(self.conn, [vid_video, vid_audio]), 1)
        self.assertEqual(self.conn.execute(
            "SELECT status FROM videos WHERE id=?", (vid_audio,)).fetchone()["status"],
            "known")
        self.assertEqual(self.conn.execute(
            "SELECT status FROM videos WHERE id=?", (vid_video,)).fetchone()["status"],
            "queued")

    def test_enqueue_include_audio_queues_audio(self):
        vid_audio = self.add_row("vid00000011", "Трек", kind="audio")
        self.assertEqual(repo.enqueue(self.conn, [vid_audio],
                                      include_audio=True), 1)
        self.assertEqual(self.conn.execute(
            "SELECT status FROM videos WHERE id=?", (vid_audio,)).fetchone()["status"],
            "queued")

    def test_enqueue_playlist_skips_audio(self):
        now = now_iso()
        cur = self.conn.execute(
            """INSERT INTO playlists (platform, remote_id, title,
                                     created_at, updated_at)
               VALUES ('soundcloud','pl1','Сборник',?,?)""", (now, now))
        playlist_id = int(cur.lastrowid)
        vid_video = self.add_row("vid00000012", "Клип")
        vid_audio = self.add_row("vid00000013", "Трек", kind="audio")
        for position, vid in ((1, vid_video), (2, vid_audio)):
            self.conn.execute(
                "INSERT INTO playlist_items (playlist_id, video_id, position, added_at) "
                "VALUES (?,?,?,?)", (playlist_id, vid, position, now))
        self.assertEqual(repo.enqueue_playlist(self.conn, playlist_id), 1)
        self.assertEqual(self.conn.execute(
            "SELECT status FROM videos WHERE id=?", (vid_audio,)).fetchone()["status"],
            "known")


class TestLocalImportMediaKind(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "local.db")
        self.conn = self.db.conn
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.db.close)

    def test_insert_local_audio_row(self):
        path = Path(self._tmp.name) / "Трек.wav"
        path.write_bytes(b"RIFF-fake")
        vid = repo.insert_local_video(
            self.conn, title="Трек", path=str(path), size=9, mtime=1.0,
            digest=None, media_kind="audio", duration_s=6.2,
            format_json=audio_mod.dumps({"media": "audio", "format": ".wav"}))
        row = dict(self.conn.execute(
            "SELECT platform, status, media_kind, duration_s FROM videos WHERE id=?",
            (vid,)).fetchone())
        self.assertEqual(row["platform"], "local")
        self.assertEqual(row["status"], "downloaded")
        self.assertEqual(row["media_kind"], "audio")
        self.assertAlmostEqual(row["duration_s"], 6.2)
        # Метки аудио лежат в строке files того же файла.
        file_row = self.conn.execute(
            "SELECT kind, format_json FROM files WHERE video_id=?", (vid,)).fetchone()
        self.assertEqual(file_row["kind"], "video")
        self.assertEqual(json.loads(file_row["format_json"])["media"], "audio")

    def test_insert_local_bad_kind_falls_back_to_video(self):
        path = Path(self._tmp.name) / "Клип.mp4"
        path.write_bytes(b"mp4")
        vid = repo.insert_local_video(self.conn, title="Клип", path=str(path),
                                      size=3, mtime=1.0, digest=None,
                                      media_kind="wat")
        self.assertEqual(self.conn.execute(
            "SELECT media_kind FROM videos WHERE id=?", (vid,)).fetchone()["media_kind"],
            "video")


class GuiAudioCase(unittest.TestCase):
    """Профиль (настройки и БД) переносим во временную папку."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        patcher = mock.patch("app.settings.settings_path",
                             return_value=self.dir / "settings.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch("app.db.db_path", return_value=self.dir / "library.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch("app.gui.log_path",
                             return_value=self.dir / "omnistash.log")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)
        # Как при реальном старте: load() создаёт файл со значениями схемы.
        from app import settings as settings_mod
        settings_mod.load()

    def make_api(self) -> Api:
        api = Api()
        self.addCleanup(api.close)
        return api

    def add_audio_row(self, api, remote_id, title="Трек", status="known"):
        vid, _ = repo.upsert_video(api.db.conn, {
            "platform": "soundcloud", "remote_id": remote_id,
            "key": f"soundcloud:{remote_id}", "title": title,
            "raw_json": "{}", "origin": "yt-dlp", "media_kind": "audio"})
        api.db.conn.execute("UPDATE videos SET status=? WHERE id=?", (status, vid))
        return vid

    def add_video_row(self, api, remote_id, title="Клип"):
        vid, _ = repo.upsert_video(api.db.conn, {
            "platform": "youtube", "remote_id": remote_id,
            "key": f"youtube:{remote_id}", "title": title,
            "raw_json": "{}", "origin": "yt-dlp"})
        return vid


class TestSettingsShowAudio(GuiAudioCase):
    def test_schema_has_show_audio_default_true(self):
        api = self.make_api()
        field = next(f for f in api.get_initial()["schema"]
                     if f["key"] == "show_audio")
        self.assertTrue(field["default"])
        self.assertEqual(api.poll(0)["settings"]["show_audio"], True)

    def test_setting_hides_audio_in_list(self):
        api = self.make_api()
        vid_audio = self.add_audio_row(api, "sc0000000001")
        vid_video = self.add_video_row(api, "yt0000000001")
        common = api.list_videos({"limit": 50})
        self.assertEqual({row["id"] for row in common["rows"]},
                         {vid_audio, vid_video})
        api.save_setting({"key": "show_audio", "value": False})
        common = api.list_videos({"limit": 50})
        self.assertEqual([row["id"] for row in common["rows"]], [vid_video])

    def test_explicit_filter_wins_over_setting(self):
        api = self.make_api()
        vid_audio = self.add_audio_row(api, "sc0000000002")
        self.add_video_row(api, "yt0000000002")
        api.save_setting({"key": "show_audio", "value": False})
        # Явный выбор человека из toolbar важнее настройки.
        only_audio = api.list_videos({"media_kind": "audio", "limit": 50})
        self.assertEqual([row["id"] for row in only_audio["rows"]], [vid_audio])
        only_video = api.list_videos({"media_kind": "video", "limit": 50})
        self.assertEqual(len(only_video["rows"]), 1)


class TestGuiOpenAndQueue(GuiAudioCase):
    def test_open_audio_file_in_player(self):
        api = self.make_api()
        path = self.dir / "Трек.mp3"
        path.write_bytes(b"ID3fake")
        vid = repo.insert_local_video(api.db.conn, title="Трек", path=str(path),
                                     size=8, mtime=1.0, digest=None,
                                     media_kind="audio")
        with mock.patch("os.startfile") as startfile:
            result = api.open_file({"id": vid})
        self.assertTrue(result.get("ok"), result)
        startfile.assert_called_once_with(str(path))

    def test_open_missing_audio_honest_hint(self):
        api = self.make_api()
        vid = self.add_audio_row(api, "sc0000000003")
        result = api.open_file({"id": vid})
        # Честная подсказка по типу строки: «аудио», а не «видео».
        self.assertIn("аудио", result["error"])
        self.assertIn("не скачано", result["error"])

    def test_gui_enqueue_is_explicit_human_action(self):
        api = self.make_api()
        out = self.dir / "out"
        out.mkdir()
        from app import storages
        storage = storages.add(api.db.conn, str(out))
        api.save_setting({"key": "default_storage_id", "value": storage["id"]})

        def fake_download(video, settings, *, stop, on_progress=None, dest=None,
                          **kwargs):
            target = Path(dest or out)
            target.mkdir(parents=True, exist_ok=True)
            path = target / "готово.mp3"
            path.write_bytes(b"payload")
            return {"cancelled": False, "files": [(str(path), "video")],
                    "info": {}, "error": None, "hash": "sha256:aa"}

        vid_audio = self.add_audio_row(api, "sc0000000004")
        with mock.patch("app.downloader.download", side_effect=fake_download):
            result = api.enqueue({"ids": [vid_audio], "storage_id": storage["id"]})
            self.assertEqual(result["queued"], 1)  # явная кнопка человека
            deadline = time.time() + 10.0
            row = None
            while time.time() < deadline:
                row = api.db.conn.execute(
                    "SELECT status, media_kind FROM videos WHERE id=?",
                    (vid_audio,)).fetchone()
                if row["status"] == "downloaded":
                    break
                time.sleep(0.05)
            self.assertEqual(row["status"], "downloaded")
            # Тип строки после загрузки остаётся audio.
            self.assertEqual(row["media_kind"], "audio")
            # Файл уже на диске - перекачки «само» нет: enqueue не трогает.
            again = api.enqueue({"ids": [vid_audio], "storage_id": storage["id"]})
            self.assertEqual(again["queued"], 0)


class CountingReader:
    """Файл, который запоминает, сколько его просили прочитать."""

    def __init__(self, real):
        self.real = real
        self.requested: list[int] = []

    def read(self, n=-1):
        self.requested.append(-1 if n is None else int(n))
        return self.real.read(n)

    def seek(self, *args):
        return self.real.seek(*args)

    def tell(self):
        return self.real.tell()

    def __enter__(self):
        self.real.__enter__()
        return self

    def __exit__(self, *exc):
        return self.real.__exit__(*exc)


class TestMp3TagReadSizing(unittest.TestCase):
    """mp3 читается по размеру блока ID3, а не «1 МиБ на каждый файл» (ревью #18).

    Размер блока лежит в 4 байтах заголовка, поэтому чтение должно быть
    ровно таким же, как сам блок: на библиотеке из тысяч аудио прежний
    приём читал лишние гигабайты, и полнее теги от этого не становились.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def write(self, name: str, payload: bytes) -> Path:
        path = self.dir / name
        path.write_bytes(payload)
        return path

    def read_tags_counted(self, path: Path):
        """read_tags по файлу + список «сколько просили прочитать»."""
        real_open = open
        readers: list[CountingReader] = []

        def patched(target, mode="rb", **kwargs):
            handle = real_open(target, mode, **kwargs)
            if mode != "rb":      # запись обложек и прочее не считаем
                return handle
            reader = CountingReader(handle)
            readers.append(reader)
            return reader

        with mock.patch("builtins.open", patched):
            found = audio_mod.read_tags(path)
        requested = [size for reader in readers for size in reader.requested]
        return found, requested

    def test_small_tag_does_not_read_a_megabyte(self):
        tag = id3v2([("TIT2", text_frame("Джазовый мик")),
                     ("APIC", apic_frame(b"\xff\xd8\xff" + b"jpeg" * 20))])
        path = self.write("мик.mp3", tag + b"\xff\xfb\x90\x00" * 300_000)
        found, requested = self.read_tags_counted(path)
        self.assertEqual(found["title"], "Джазовый мик")
        self.assertEqual(found["tags"], "id3v2")
        self.assertLess(max(requested), audio_mod.TAG_BLOCK_MAX_BYTES,
                        "теги не обязаны читать мегабайт дорожки")
        self.assertLessEqual(max(requested), len(tag))

    def test_giant_declared_block_is_capped(self):
        """Заявленный блок больше потолка - читаем потолок, теги всё равно читаются."""
        body = b"TIT2" + len(text_frame("Гигант")).to_bytes(4, "big") \
            + b"\x00\x00" + text_frame("Гигант")
        header = b"ID3" + b"\x03\x00\x00" + sync_safe(2 * 1024 * 1024)
        path = self.write("гигант.mp3", header + body + b"\xff\xfb\x90\x00" * 1000)
        found, requested = self.read_tags_counted(path)
        self.assertEqual(found["title"], "Гигант")
        self.assertEqual(max(requested),
                         audio_mod.TAG_BLOCK_MAX_BYTES - audio_mod.ID3_HEADER_BYTES)

    def test_declared_block_larger_than_the_file_still_parses(self):
        """Блок заявлен длиннее файла: разбор идёт по фактическим байтам."""
        body = b"TIT2" + len(text_frame("Хвост")).to_bytes(4, "big") \
            + b"\x00\x00" + text_frame("Хвост")
        header = b"ID3" + b"\x03\x00\x00" + sync_safe(64 * 1024)
        path = self.write("неполный.mp3", header + body)
        found, _requested = self.read_tags_counted(path)
        self.assertEqual(found["title"], "Хвост")
        self.assertEqual(found["tags"], "id3v2")

    def test_padding_inside_the_block_is_read_by_size(self):
        """Padding внутри блока (многие теггеры) читается и не мешает разбору."""
        body = b""
        for key, payload in [("TIT2", text_frame("С запасом")),
                             ("TPE1", text_frame("Квинтет"))]:
            body += key.encode("latin-1") + len(payload).to_bytes(4, "big") \
                + b"\x00\x00" + payload
        padding = b"\x00" * 100_000
        header = b"ID3" + b"\x03\x00\x00" + sync_safe(len(body) + len(padding))
        path = self.write("запас.mp3", header + body + padding
                          + b"\xff\xfb\x90\x00" * 5000)
        found, requested = self.read_tags_counted(path)
        self.assertEqual(found["title"], "С запасом")
        self.assertEqual(found["artist"], "Квинтет")
        self.assertLessEqual(max(requested), len(header) + len(body) + len(padding))

    def test_no_id3v2_falls_back_to_id3v1(self):
        tail = (b"TAG" + b"Mix".ljust(30, b" ") + b"Artist".ljust(30, b" ")
                + b"Album".ljust(30, b" ") + b"2024" + b" ".ljust(28, b" ")
                + b"\x00" + b"\x05" + b"\x0c")
        self.assertEqual(len(tail), 128)
        path = self.write("старый.mp3", b"\xff\xfb\x90\x00" * 500 + tail)
        found, requested = self.read_tags_counted(path)
        self.assertEqual(found["tags"], "id3v1")
        self.assertEqual(found["title"], "Mix")
        self.assertLess(max(requested), audio_mod.TAG_BLOCK_MAX_BYTES)

    def test_tiny_file_is_not_a_tag(self):
        path = self.write("малыш.mp3", b"ID3")
        found, _requested = self.read_tags_counted(path)
        self.assertIsNone(found)
