"""Перекодировка в HEVC: кодировщики, регистрация PP, судьба файла.

ffmpeg здесь не запускается по-настоящему: вывод `ffmpeg -encoders` и
`run_ffmpeg` подменяются, потому что в тестах нет ни ffmpeg, ни файлов
для настоящей перекодировки. Жизненный путь проверяем на фейковом вызове,
который пишет выходной файл ровно так же, как это сделал бы ffmpeg.
"""

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from app import downloader
from app import settings_schema
from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessorError


class TestSchemaMatchesTranscoders(unittest.TestCase):
    def test_choices_are_none_plus_transcoder_keys(self):
        spec = settings_schema.field("transcode")
        values = [value for value, _label in spec["choices"]]
        self.assertEqual(values, ["none", *downloader.TRANSCODERS.keys()],
                         "схема и кодировщики должны быть одним списком")


class TestAvailableTranscoders(unittest.TestCase):
    def setUp(self):
        downloader._ENCODER_CACHE.clear()
        self.addCleanup(downloader._ENCODER_CACHE.clear)
        handle, self.ffmpeg = tempfile.mkstemp(suffix=".exe")
        os.close(handle)

    def _run(self, stdout: str, calls=None):
        def fake_run(args, **kwargs):
            if calls is not None:
                calls.append(list(args))
            return mock.Mock(stdout=stdout, returncode=0)

        with mock.patch.object(downloader.subprocess, "run",
                               side_effect=fake_run):
            return downloader.available_transcoders(self.ffmpeg)

    def test_parses_encoders_listing(self):
        listing = ("Encoders:\n V..... libx265              libx265 H.265\n"
                   " V..... hevc_nvenc            NVIDIA NVENC H.265\n")
        self.assertEqual(self._run(listing), ["libx265", "nvenc"])

    def test_build_without_encoders_is_empty(self):
        self.assertEqual(self._run("Encoders:\n V..... mpeg4\n"), [])

    def test_broken_ffmpeg_is_empty(self):
        with mock.patch.object(downloader.subprocess, "run",
                               side_effect=OSError("не запустился")):
            self.assertEqual(
                downloader.available_transcoders(self.ffmpeg), [])

    def test_no_ffmpeg_is_empty(self):
        with mock.patch.object(downloader, "find_ffmpeg", return_value=None):
            self.assertEqual(downloader.available_transcoders(None), [])

    def test_cache_hit_then_refresh_on_mtime_change(self):
        calls = []
        listing = " V..... libx265\n"
        first = self._run(listing, calls)
        second = self._run(listing, calls)
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1, "второй запрос - из кэша")
        future = os.path.getmtime(self.ffmpeg) + 60
        os.utime(self.ffmpeg, (future, future))
        self._run(listing, calls)
        self.assertEqual(len(calls), 2, "после переустановки (mtime) - заново")


class _QuietPPMixin:
    @staticmethod
    def make_pp(encoder: str):
        pp = downloader.TranscodePP(None, encoder=encoder)
        pp.to_screen = lambda *args, **kwargs: None   # журнал тестам не нужен
        return pp


class TestTranscodePP(_QuietPPMixin, unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.folder = Path(self.dir.name)
        self.src = self.folder / "video.mp4"
        self.src.write_bytes("исходник".encode())
        self.info = {
            "ext": "mp4",
            "filepath": str(self.src),
            "_filename": str(self.src),
            "requested_downloads": [{"filepath": str(self.src),
                                     "_filename": str(self.src)}],
        }

    def _fake_ffmpeg(self, pp, *, fail_for=(), codecs_seen=None):
        def run_ffmpeg(src, dst, options):
            codec = options[options.index("-c:v") + 1]
            if codecs_seen is not None:
                codecs_seen.append(codec)
            if codec in fail_for:
                raise FFmpegPostProcessorError(f"{codec} недоступен")
            Path(dst).write_bytes("перекодировано".encode())

        pp.run_ffmpeg = run_ffmpeg

    def test_renames_and_removes_original_and_updates_info(self):
        thumb = self.folder / "video.webp"
        thumb.write_bytes("обложка".encode())
        pp = self.make_pp("libx265")
        seen = []
        self._fake_ffmpeg(pp, codecs_seen=seen)

        pp.run(self.info)

        out = self.folder / "video [HEVC].mp4"
        self.assertEqual(seen, ["libx265"])
        self.assertTrue(out.is_file(), "должен появиться файл с [HEVC]")
        self.assertFalse(self.src.is_file(), "оригинал убран - копий не остаётся")
        self.assertEqual(self.info["filepath"], str(out))
        self.assertEqual(self.info["requested_downloads"][0]["filepath"],
                         str(out), "очередь должна увидеть новый путь")
        self.assertTrue((self.folder / "video [HEVC].webp").is_file(),
                        "обложка переименована следом за видео")
        self.assertEqual([p.name for p in self.folder.iterdir()],
                         sorted(["video [HEVC].mp4", "video [HEVC].webp"]),
                         "временных файлов не остаётся")

    def test_gpu_falls_back_to_libx265(self):
        pp = self.make_pp("nvenc")
        seen = []
        self._fake_ffmpeg(pp, fail_for=("hevc_nvenc",), codecs_seen=seen)

        pp.run(self.info)

        self.assertEqual(seen, ["hevc_nvenc", "libx265"],
                         "сначала GPU, потом CPU")
        self.assertTrue((self.folder / "video [HEVC].mp4").is_file())
        self.assertFalse(self.src.is_file())

    def test_all_failed_keeps_original(self):
        pp = self.make_pp("nvenc")
        seen = []
        self._fake_ffmpeg(pp, fail_for=("hevc_nvenc", "libx265"),
                          codecs_seen=seen)

        pp.run(self.info)

        self.assertTrue(self.src.is_file(),
                        "не перекодилось - исходник обязан остаться")
        self.assertEqual(self.info["filepath"], str(self.src),
                         "info не должен врать о пути")
        self.assertEqual([p.name for p in self.folder.iterdir()], ["video.mp4"],
                         "временные файлы подчищены")

    def test_non_mp4_is_skipped(self):
        pp = self.make_pp("libx265")
        called = []
        pp.run_ffmpeg = lambda *a, **k: called.append(a)
        info = dict(self.info, ext="webm")

        pp.run(info)

        self.assertEqual(called, [], "webm не трогаем")
        self.assertTrue(self.src.is_file())

    def test_already_hevc_name_is_noop(self):
        hevc = self.folder / "video [HEVC].mp4"
        hevc.write_bytes("уже".encode())
        pp = self.make_pp("libx265")
        called = []
        pp.run_ffmpeg = lambda *a, **k: called.append(a)

        pp.run(dict(self.info, filepath=str(hevc), _filename=str(hevc)))

        self.assertEqual(called, [], "повторный суффикс не добавляем")


class TestDownloadRegistersTranscode(unittest.TestCase):
    """download(): PP регистрируется только при перекодировке И ffmpeg."""

    class FakeYDL:
        created = []
        params = {}          # у настоящего ydl есть: PP читает ffmpeg_location

        def __init__(self, opts):
            self.opts = opts
            self.pps = []
            type(self).created.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def add_post_processor(self, pp, when=None):
            self.pps.append((pp, when))

        def extract_info(self, url, download=True):
            return {"ext": "mp4", "filepath": "/tmp/x.mp4"}

    def setUp(self):
        self.FakeYDL.created = []
        patcher = mock.patch.object(downloader.yt_dlp, "YoutubeDL",
                                    self.FakeYDL)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.video = {"remote_id": "abcdefghijk",
                      "webpage_url": "https://example.invalid/v"}

    def _download(self, settings):
        return downloader.download(
            self.video, settings, stop=threading.Event(),
            dest=self.tmp.name)

    def test_transcode_with_ffmpeg_registers_pp(self):
        with mock.patch.object(downloader, "find_ffmpeg",
                               return_value="C:/fake/ffmpeg.exe"):
            self._download({"transcode": "libx265"})
        ydl = self.FakeYDL.created[-1]
        kinds = [type(pp).__name__ for pp, _when in ydl.pps]
        self.assertIn("TranscodePP", kinds)
        self.assertEqual(ydl.opts["ffmpeg_location"], "C:/fake/ffmpeg.exe")

    def test_no_transcode_means_no_pp(self):
        with mock.patch.object(downloader, "find_ffmpeg",
                               return_value="C:/fake/ffmpeg.exe"):
            self._download({})
        ydl = self.FakeYDL.created[-1]
        self.assertEqual([pp for pp, _in in ydl.pps], [])

    def test_transcode_without_ffmpeg_is_skipped_and_hls_fixed(self):
        # Перекодировка без ffmpeg - не ошибка: качаем, HLS чиним mpegts.
        with mock.patch.object(downloader, "find_ffmpeg", return_value=None):
            result = self._download({"transcode": "libx265"})
        self.assertFalse(result.get("error"))
        ydl = self.FakeYDL.created[-1]
        self.assertEqual([pp for pp, _in in ydl.pps], [])
        self.assertTrue(ydl.opts.get("hls_use_mpegts"),
                        "без ffmpeg HLS должен идти готовым потоком")


if __name__ == "__main__":
    unittest.main()
