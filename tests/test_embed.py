"""Режим «всё в видео»: postprocessor'ы, агрегат вместо сайдкара.

Сеть подменяется фикстурой FakeYDL: она пишет настоящий файл, как это
сделал бы yt-dlp, и запоминает зарегистрированные постпроцессоры - тесты
проверяют и то, ЧТО встраивается (обложка+метаданные стадией after_move,
после перекодировки), и то, ЧТО остаётся на диске (mp4 + агрегат папки).
"""

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from app import aggregate, downloader


class FakeYDL:
    """Фальшивый YoutubeDL: создаёт файл по outtmpl, ловит PP."""

    created: list = []

    def __init__(self, opts):
        self.opts = opts
        self.params = opts        # yt-dlp читает параметры через downloader.params
        self.pps = []
        FakeYDL.created.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def add_post_processor(self, pp, when=None):
        self.pps.append((pp, when))

    # yt-dlp-шные доклады: PP создается с нашим объектом и может писать
    # в «консоль» - заглушаем бездельем.
    def to_screen(self, *args, **kwargs):
        pass

    def report_warning(self, *args, **kwargs):
        pass

    def report_error(self, *args, **kwargs):
        raise AssertionError(f"yt-dlp error in fake: {args}")

    def write_debug(self, *args, **kwargs):
        pass

    def extract_info(self, url, download=True):
        outtmpl = self.opts.get("outtmpl") or ""
        path = outtmpl % {"title": "Ролик", "id": "abcdefghijk",
                          "ext": "mp4", "channel": "Автор",
                          "upload_date": "20250101"}
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"video-bytes")
        return {"id": "abcdefghijk", "title": "Ролик", "ext": "mp4",
                "webpage_url": url, "filepath": path,
                "requested_downloads": [{"filepath": path}]}


class EmbedCase(unittest.TestCase):
    def setUp(self):
        FakeYDL.created = []
        patcher = mock.patch.object(downloader.yt_dlp, "YoutubeDL", FakeYDL)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.video = {"remote_id": "abcdefghijk",
                      "webpage_url": "https://example.invalid/v"}

    def _download(self, settings, ffmpeg="C:/fake/ffmpeg.exe"):
        with mock.patch.object(downloader, "find_ffmpeg",
                               return_value=ffmpeg):
            return downloader.download(
                self.video, settings, stop=threading.Event(),
                dest=self.tmp.name)

    def _settings(self, mode):
        return {"output_template": "%(title)s [%(id)s].%(ext)s",
                "sidecar_mode": mode}


class TestPostProcessors(EmbedCase):
    def test_embed_registers_metadata_and_thumbnail_after_move(self):
        # Встроение - стадией after_move: ПОСЛЕ перекодировки, иначе
        # HEVC-перекодчик потерял бы встроенную обложку.
        result = self._download(self._settings("embed"))
        self.assertFalse(result["error"])
        ydl = FakeYDL.created[-1]
        kinds = [(type(pp).__name__, when) for pp, when in ydl.pps]
        self.assertEqual(kinds, [("FFmpegMetadataPP", "after_move"),
                                 ("EmbedThumbnailPP", "after_move")])

    def test_files_mode_registers_no_embed_pps(self):
        self._download({})
        self.assertEqual(FakeYDL.created[-1].pps, [])

    def test_embed_without_ffmpeg_skips_pps_but_keeps_metadata(self):
        # Без ffmpeg встроить нечем - но запись в агрегат всё равно
        # пишется: метаданные библиотеки не должны пропасть.
        result = self._download(self._settings("embed"), ffmpeg=None)
        self.assertFalse(result["error"])
        self.assertEqual(FakeYDL.created[-1].pps, [])
        self.assertIsNotNone(aggregate.find(Path(self.tmp.name), "abcdefghijk"),
                             "без ffmpeg запись агрегата обязательна")


class TestDiskResult(EmbedCase):
    def test_embed_leaves_mp4_plus_aggregate(self):
        result = self._download(self._settings("embed"))
        self.assertFalse(result["error"])
        self.assertFalse(result["cancelled"])
        kinds = sorted(kind for _path, kind in result["files"])
        self.assertEqual(kinds, ["video"], "рядом ничего не должно лежать")

        media = Path(self.tmp.name) / "Ролик [abcdefghijk].mp4"
        self.assertTrue(media.exists())
        self.assertFalse(media.with_suffix(".post.json").exists(),
                         "пофайловый сайдкар в embed-режиме не пишется")

        record = aggregate.find(self.tmp.name, "abcdefghijk")
        self.assertIsNotNone(record)
        self.assertEqual(record["path"], "Ролик [abcdefghijk].mp4",
                         "в записи - имя файла, а не абсолютный путь")
        self.assertEqual(record["info"]["title"], "Ролик")
        self.assertTrue(record["hash"].startswith("sha256:"),
                        "хеш считается после встраивания - он про файл")
        # Всё, что нужно для восстановления, - в одном json на папку.
        raw = json.loads((Path(self.tmp.name) /
                          aggregate.FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(set(raw["videos"]), {"abcdefghijk"})

    def test_files_mode_writes_sidecar_as_before(self):
        result = self._download({})
        self.assertFalse(result["error"])
        kinds = sorted(kind for _path, kind in result["files"])
        self.assertEqual(kinds, ["sidecar", "video"])
        sidecar = Path(self.tmp.name) / "Ролик [abcdefghijk].post.json"
        self.assertTrue(sidecar.exists())
        self.assertFalse((Path(self.tmp.name) / aggregate.FILENAME).exists(),
                         "в files-режиме агрегат не появляется")

    def test_embed_removes_stale_sidecar_after_redownload(self):
        # Перекачка в embed-режиме: старый сайдкар врал бы про хеш файла.
        stale = Path(self.tmp.name) / "Ролик [abcdefghijk].post.json"
        stale.write_text('{"omnistash": 1, "hash": "sha256:old"}',
                         encoding="utf-8")
        self._download(self._settings("embed"))
        self.assertFalse(stale.exists(),
                         "устаревший сайдкар должен уйти после записи")
        self.assertIsNotNone(aggregate.find(self.tmp.name, "abcdefghijk"))


class TestOpts(EmbedCase):
    def _opts(self, settings, ffmpeg="C:/fake/ffmpeg.exe"):
        with mock.patch.object(downloader, "find_ffmpeg",
                               return_value=ffmpeg):
            return downloader.build_opts(settings, Path(self.tmp.name),
                                         stop=threading.Event())

    def test_embed_forces_thumbnail_even_without_save_thumb(self):
        opts = self._opts({"sidecar_mode": "embed", "save_thumb": False})
        self.assertTrue(opts.get("writethumbnail"),
                        "обложка нужна как источник для встраивания")

    def test_files_mode_respects_save_thumb(self):
        opts = self._opts({"save_thumb": False})
        self.assertFalse(opts.get("writethumbnail"))

    def test_embed_without_ffmpeg_needs_no_thumbnail(self):
        # Встроить нечем: обложку тащить незачем (save_thumb выключен).
        opts = self._opts({"sidecar_mode": "embed", "save_thumb": False},
                          ffmpeg=None)
        self.assertFalse(opts.get("writethumbnail"))

    def test_embed_mode_reads_schema_key_only(self):
        # Словарь без ключа (старые скрипты/тесты) - режим files.
        self.assertFalse(downloader.embed_mode({}))
        self.assertTrue(downloader.embed_mode({"sidecar_mode": "embed"}))
        self.assertFalse(downloader.embed_mode({"sidecar_mode": "files"}))


if __name__ == "__main__":
    unittest.main()
