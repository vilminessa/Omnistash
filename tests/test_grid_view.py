"""Плитка библиотеки: has_thumb, пачка обложек, открытие файла и папки."""

import os
import unittest
from pathlib import Path
from unittest import mock

from app import gui as gui_mod
from app import repo
from app import settings_schema
from tests.test_gui import GuiCase

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
       b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89")


class GridViewCase(GuiCase):
    """Окно + пара «видео с обложкой» / «без» для проверок списка."""

    def setUp(self):
        super().setUp()
        self.api = self.make_api()
        self.conn = self.api.db.conn

    def _video(self, key, title, *, thumb=None, video_file=None):
        vid, _ = repo.upsert_video(self.conn, {
            "platform": "youtube", "remote_id": key, "key": f"youtube:{key}",
            "title": title, "raw_json": "{}", "origin": "yt-dlp"})
        if thumb:
            # размер берём реальный, но файл может и не существовать (тест
            # «обложка пропала после чистки диска»)
            size = thumb.stat().st_size if thumb.exists() else 1
            repo.record_file(self.conn, vid, str(thumb), "thumbnail",
                             size=size, mtime=1.0)
        if video_file:
            repo.record_file(self.conn, vid, str(video_file), "video",
                             size=video_file.stat().st_size, mtime=1.0)
        return vid


class TestHasThumbInList(GridViewCase):
    def test_flag_reflects_local_thumbnail(self):
        (self.dir / "a.png").write_bytes(PNG)
        with_thumb = self._video("aaaaaaaaaaa", "с обложкой",
                                 thumb=self.dir / "a.png")
        without = self._video("bbbbbbbbbbb", "без обложки")

        rows = {row["id"]: row for row in
                repo.list_videos(self.conn)["rows"]}
        self.assertTrue(rows[with_thumb]["has_thumb"])
        self.assertFalse(rows[without]["has_thumb"])

    def test_missing_thumbnail_does_not_count(self):
        # Файл записан, но помечен пропавшим - обложки нет.
        vid = self._video("ccccccccccc", "обложка пропала")
        repo.record_file(self.conn, vid, str(self.dir / "gone.png"),
                         "thumbnail", size=1, mtime=1.0)
        self.conn.execute("UPDATE files SET missing=1 WHERE video_id=?", (vid,))

        rows = {row["id"]: row for row in repo.list_videos(self.conn)["rows"]}
        self.assertFalse(rows[vid]["has_thumb"])


class TestGetThumbs(GridViewCase):
    def test_batch_returns_data_uris_for_existing_only(self):
        (self.dir / "a.png").write_bytes(PNG)
        (self.dir / "b.png").write_bytes(PNG)
        vid_a = self._video("aaaaaaaaaaa", "a", thumb=self.dir / "a.png")
        vid_b = self._video("bbbbbbbbbbb", "b", thumb=self.dir / "b.png")
        vid_c = self._video("ccccccccccc", "c")           # без обложки

        result = self.api.get_thumbs({"ids": [vid_a, vid_b, vid_c]})
        self.assertNotIn("error", result)
        thumbs = result["thumbs"]
        self.assertEqual(set(thumbs), {str(vid_a), str(vid_b)},
                         "без обложки в ответе нет и запросить её незачем")
        self.assertTrue(thumbs[str(vid_a)].startswith("data:image/png;base64,"))

    def test_broken_file_is_skipped_silently(self):
        vid = self._video("aaaaaaaaaaa", "битая",
                          thumb=self.dir / "не-существует.png")
        # record_file принимает путь без проверки существования - как в жизни
        # после чистки диска.
        result = self.api.get_thumbs({"ids": [vid]})
        self.assertEqual(result["thumbs"], {},
                         "битая обложка не должна ронять пачку")

    def test_limit_is_enforced(self):
        result = self.api.get_thumbs({"ids": list(range(1, 102))})
        self.assertIn("error", result)
        self.assertIn("слишком много", result["error"])

    def test_empty_and_dupe_ids(self):
        self.assertEqual(self.api.get_thumbs({})["thumbs"], {})
        self.assertEqual(self.api.get_thumbs({"ids": []})["thumbs"], {})
        (self.dir / "a.png").write_bytes(PNG)
        vid = self._video("aaaaaaaaaaa", "a", thumb=self.dir / "a.png")
        result = self.api.get_thumbs({"ids": [vid, vid, vid]})
        self.assertEqual(set(result["thumbs"]), {str(vid)})


class TestOpenFileAndFolder(GridViewCase):
    def _downloaded(self, *, on_disk=True):
        path = self.dir / "видео [aaaaaaaaaaa].mp4"
        path.write_bytes("видео".encode())
        vid = self._video("aaaaaaaaaaa", "скачано", video_file=path)
        if not on_disk:
            path.unlink()
        return vid, path

    def test_open_file_uses_indexed_path(self):
        vid, path = self._downloaded()
        with mock.patch.object(gui_mod.os, "startfile") as startfile:
            result = self.api.open_file({"id": vid})
        self.assertTrue(result["ok"], result)
        startfile.assert_called_once_with(str(path))
        self.assertTrue(any("Открыто:" in line for line in self.api._logs))

    def test_open_file_when_not_downloaded(self):
        vid = self._video("aaaaaaaaaaa", "не скачано")
        with mock.patch.object(gui_mod.os, "startfile") as startfile:
            result = self.api.open_file({"id": vid})
        self.assertIn("не скачано", result["error"])
        startfile.assert_not_called()

    def test_open_file_detects_lost_file(self):
        vid, _ = self._downloaded(on_disk=False)
        with mock.patch.object(gui_mod.os, "startfile") as startfile:
            result = self.api.open_file({"id": vid})
        self.assertIn("пропал", result["error"])
        startfile.assert_not_called()

    def test_open_folder_selects_the_file(self):
        vid, path = self._downloaded()
        with mock.patch.object(gui_mod.subprocess, "Popen") as popen:
            result = self.api.open_folder({"id": vid})
        self.assertTrue(result["ok"], result)
        args = popen.call_args[0][0]
        self.assertEqual(args[:2], ["explorer", "/select,"])
        self.assertEqual(args[2], os.path.normpath(str(path)))

    def test_bad_id_is_rejected(self):
        self.assertIn("error", self.api.open_file({"id": "не-число"}))
        self.assertIn("error", self.api.open_file({}))


class TestViewSettings(unittest.TestCase):
    def test_view_mode_and_tile_size_in_schema(self):
        view = settings_schema.field("view_mode")
        self.assertEqual([v for v, _l in view["choices"]], ["list", "grid"])
        size = settings_schema.field("tile_size")
        self.assertEqual([v for v, _l in size["choices"]],
                         ["small", "medium", "large"])
        self.assertEqual(view["default"], "list")
        self.assertEqual(size["default"], "medium")


if __name__ == "__main__":
    unittest.main()
