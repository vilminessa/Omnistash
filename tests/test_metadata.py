"""Нормализация метаданных: ключи, даты, разбор ссылок, «худой» raw_json."""

import json
import unittest

from app.metadata import (classify_url, normalize_video, playlist_kind,
                          slim_info, video_key)
from app.util import human_duration, human_size, iso_date, sanitize_name


class TestIsoDate(unittest.TestCase):
    def test_yyyymmdd(self):
        self.assertEqual(iso_date("20250101"), "2025-01-01")

    def test_iso_with_time(self):
        self.assertEqual(iso_date("2025-01-01T12:00:00Z"), "2025-01-01")

    def test_garbage_is_none(self):
        # Мусор не должен превращаться в «0001-01-01» и ломать сортировку.
        self.assertIsNone(iso_date("вчера"))
        self.assertIsNone(iso_date(""))
        self.assertIsNone(iso_date(None))
        self.assertIsNone(iso_date("2025-13-45"))


class TestClassifyUrl(unittest.TestCase):
    LIST = "PLrAXtmRdnEQy6nuLMOV8u4-y_q0Q_hG7x"

    def test_watch_is_video(self):
        info = classify_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(info["kind"], "video")
        self.assertEqual(info["video_id"], "dQw4w9WgXcQ")

    def test_watch_with_list_is_playlist(self):
        # Так ссылки и вставляют: watch?v=...&list=... - имеется в виду список.
        info = classify_url(
            f"https://www.youtube.com/watch?v=dQw4w9WgXcQ&list={self.LIST}")
        self.assertEqual(info["kind"], "playlist")
        self.assertEqual(info["playlist_id"], self.LIST)
        self.assertEqual(info["video_id"], "dQw4w9WgXcQ")

    def test_playlist_url(self):
        info = classify_url(f"https://www.youtube.com/playlist?list={self.LIST}")
        self.assertEqual(info["kind"], "playlist")

    def test_short_link(self):
        info = classify_url("https://youtu.be/dQw4w9WgXcQ?t=42")
        self.assertEqual(info["kind"], "video")

    def test_handle_channel(self):
        info = classify_url("https://www.youtube.com/@SomeChannel/videos")
        self.assertEqual(info["kind"], "channel")
        self.assertEqual(info["handle"], "SomeChannel")

    def test_channel_id(self):
        info = classify_url("https://www.youtube.com/channel/UCuAXFkgsw1L7xaCfnd5JJOw")
        self.assertEqual(info["kind"], "channel")
        self.assertEqual(info["channel_id"], "UCuAXFkgsw1L7xaCfnd5JJOw")

    def test_channel_with_uploads_playlist(self):
        # Загрузки канала - playlist UU...: подписываем источник как «канал».
        info = classify_url(
            "https://www.youtube.com/playlist?list=UUuAXFkgsw1L7xaCfnd5JJOw")
        self.assertEqual(info["kind"], "playlist")

    def test_garbage(self):
        self.assertEqual(classify_url("просто текст")["kind"], "unknown")

    def test_mix_detection(self):
        self.assertEqual(playlist_kind("RDMMdQw4w9WgXcQ"), "mix")
        self.assertEqual(playlist_kind("PLrAXtmRdnEQy6nuLMOV8u4"), "remote")


class TestNormalizeVideo(unittest.TestCase):
    def test_columns(self):
        data = normalize_video({
            "id": "dQw4w9WgXcQ", "title": "  Клип  ",
            "upload_date": "20091025", "duration": 213, "view_count": "100",
            "description": "", "channel_id": "UCuAXFkgsw1L7xaCfnd5JJOw",
            "channel": "Autor", "thumbnail": "https://i.ytimg.com/x.jpg",
            "formats": [{"format_id": "18"}] * 50,
        })
        self.assertEqual(data["key"], video_key("youtube", "dQw4w9WgXcQ"))
        self.assertEqual(data["title"], "Клип")            # без лишних пробелов
        self.assertEqual(data["uploaded_at"], "2009-10-25")
        self.assertEqual(data["duration_s"], 213)
        self.assertEqual(data["view_count"], 100)
        self.assertIsNone(data["description"])              # "" -> None
        self.assertEqual(data["channel"]["remote_id"], "UCuAXFkgsw1L7xaCfnd5JJOw")
        self.assertEqual(data["thumb_url"], "https://i.ytimg.com/x.jpg")

    def test_slim_drops_bulk(self):
        # Списки форматов и субтитров не должны жить в БД: 50 форматов -
        # это десятки килобайт на каждое видео.
        raw = slim_info({"title": "A", "formats": [{"x": 1}] * 50,
                         "automatic_captions": {"en": ["u"] * 30},
                         "description": "text"})
        parsed = json.loads(raw)
        self.assertNotIn("formats", parsed)
        self.assertNotIn("automatic_captions", parsed)
        self.assertEqual(parsed["formats_count"], 50)
        self.assertEqual(parsed["captions_available"], ["en"])
        self.assertEqual(parsed["description"], "text")

    def test_missing_channel_is_none(self):
        data = normalize_video({"id": "x", "title": "t"})
        self.assertIsNone(data["channel"])


class TestFormats(unittest.TestCase):
    def test_human_size(self):
        self.assertEqual(human_size(512), "512 Б")
        self.assertEqual(human_size(2048), "2.0 КиБ")

    def test_human_duration(self):
        self.assertEqual(human_duration(90), "1:30")
        self.assertEqual(human_duration(3725), "1:02:05")
        self.assertEqual(human_duration(None), "-")

    def test_sanitize_windows_names(self):
        self.assertNotIn(":", sanitize_name("a:b/c"))
        self.assertEqual(sanitize_name("   "), "_")
        # Зарезервированные имена Windows нельзя использовать как есть.
        self.assertTrue(sanitize_name("CON").startswith("_"))
        # Хвостовая точка: Windows её молча срезает - мы лучше заранее.
        self.assertEqual(sanitize_name("имя."), "имя")


if __name__ == "__main__":
    unittest.main()
