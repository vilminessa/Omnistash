"""Нормализация метаданных: ключи, даты, разбор ссылок, «худой» raw_json."""

import json
import unittest

from app.metadata import (classify_url, flat_entry, normalize_video,
                          playlist_kind, slim_info, video_key)
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


class TestClassifyPh(unittest.TestCase):
    """PH1: классификация PornHub-ссылок (своя платформа)."""

    def test_viewkey_is_video(self):
        info = classify_url(
            "https://www.pornhub.com/view_video.php?viewkey=ph5f12ab34cd567")
        self.assertEqual(info["platform"], "pornhub")
        self.assertEqual(info["kind"], "video")
        self.assertEqual(info["video_id"], "ph5f12ab34cd567")

    def test_tag_page(self):
        info = classify_url("https://www.pornhub.com/video/tag/anal")
        self.assertEqual(info["kind"], "tag")
        self.assertEqual(info["tag"], "anal")

    def test_short_numeric_playlist(self):
        info = classify_url("https://www.pornhub.com/playlist?id=98765432")
        self.assertEqual(info["kind"], "playlist")
        self.assertEqual(info["playlist_id"], "98765432")

    def test_profiles_are_channels(self):
        for url, handle in (
                ("https://www.pornhub.com/video/uploaded?user=someguy",
                 "someguy"),
                ("https://www.pornhub.com/pornstar/some-star/videos/uploaded",
                 "some-star"),
                ("https://www.pornhub.com/model/some-model", "some-model")):
            info = classify_url(url)
            self.assertEqual(info["platform"], "pornhub", url)
            self.assertEqual(info["kind"], "channel", url)
            self.assertEqual(info["handle"], handle, url)

    def test_youtube_untouched(self):
        info = classify_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(info["platform"], "youtube")
        self.assertEqual(info["kind"], "video")


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

    def test_metrics_and_release_date(self):
        # Дата ВЫХОДА (release_date) важнее даты загрузки: у премьер они
        # расходятся, и зрелю интересует выход. Счётчики - в колонки.
        data = normalize_video({
            "id": "x", "title": "t",
            "release_date": "20210604", "upload_date": "20220714",
            "like_count": 6841, "comment_count": "38", "view_count": 5,
        })
        self.assertEqual(data["uploaded_at"], "2021-06-04")
        self.assertEqual(data["like_count"], 6841)
        self.assertEqual(data["comment_count"], 38)
        # Без release - как раньше, по дате загрузки.
        data = normalize_video({"id": "x", "upload_date": "20220714"})
        self.assertEqual(data["uploaded_at"], "2022-07-14")


class TestFlatEntry(unittest.TestCase):
    """Запись снапшота: полный режим приносит больше полей, быстрый - меньше."""

    def test_full_entry_carries_metrics(self):
        item = flat_entry({
            "id": "dQw4w9WgXcQ", "title": "Клип", "duration": 213,
            "view_count": 100, "like_count": 42, "comment_count": 7,
            "release_date": "20210604", "upload_date": "20220714",
            "description": "Описание", "category": "Музыка",
        }, position=1)
        self.assertEqual(item["remote_id"], "dQw4w9WgXcQ")
        self.assertEqual(item["uploaded_at"], "2021-06-04")
        self.assertEqual(item["like_count"], 42)
        self.assertEqual(item["comment_count"], 7)
        self.assertEqual(item["description"], "Описание")
        self.assertEqual(item["category"], "Музыка")
        # Сырой info уезжает в raw_json (в быстром режиме он тонкий,
        # но upsert full=False старую богатую карточку им не затирает).
        parsed = json.loads(item["raw"])
        self.assertEqual(parsed["title"], "Клип")

    def test_fast_entry_takes_thumb_from_thumbnails(self):
        # Плоский список не отдаёт scalar thumbnail - берём из списка.
        item = flat_entry({
            "id": "vid000000001", "title": "V", "duration": 10,
            "view_count": 5,
            "thumbnails": [{"url": "https://i.ytimg.com/1.jpg"},
                           {"url": "https://i.ytimg.com/2.jpg"}],
        }, position=2)
        self.assertEqual(item["thumb"], "https://i.ytimg.com/2.jpg")
        self.assertIsNone(item["like_count"])       # быстрый режим их не даёт
        self.assertIsNone(item["description"])

    def test_unavailable_hole(self):
        item = flat_entry(None, position=3)
        self.assertTrue(item["unavailable"])
        self.assertIsNone(item["remote_id"])


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
