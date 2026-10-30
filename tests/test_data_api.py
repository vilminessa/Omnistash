"""YouTube Data API v3 (G2): mocked ответы, квота и откат на скрейп, метки.

Сеть не ходит: _open и fetch_video мокируются; метки сравниваются с
metadata.normalize_video (нормализация одна и та же для Data API и скрейпа).
"""

import io
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

from app import data_api
from app import metadata
from app import oauth
from app import sources
from app.sources import FetchError
from tests.test_gui import GuiCase


def api_item(video_id="dQw4w9WgXcQ"):
    return {
        "id": video_id,
        "snippet": {"title": "Название", "description": "описание",
                    "channelId": "UCabcd0123456789efghijkl",
                    "channelTitle": "Автор",
                    "publishedAt": "2026-01-02T10:00:00Z",
                    "category": "27", "tags": ["музыка"],
                    "thumbnails": {"default": {"url": "https://x/d.jpg"},
                                   "high": {"url": "https://x/h.jpg"}},
                    },
        "contentDetails": {"duration": "PT1M38S"},
        "statistics": {"viewCount": "1234", "likeCount": "56"},
    }


def http_error(code, payload):
    return urllib.error.HTTPError("https://www.googleapis.com/youtube/v3/videos",
                                  code, "Forbidden", {},
                                  io.BytesIO(payload.encode("utf-8")))


def quota_error():
    return http_error(403, '{"error": {"code": 403, "message": "quota exceeded",'
                           ' "errors": [{"reason": "quotaExceeded"}]}}')


class TestIsoDuration(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(data_api.iso_duration("PT1M38S"), 98)
        self.assertEqual(data_api.iso_duration("PT2H0M5S"), 7205)
        self.assertEqual(data_api.iso_duration("PT1H"), 3600)
        self.assertEqual(data_api.iso_duration("PT0M30.5S"), 30.5)
        self.assertEqual(data_api.iso_duration("PT0S"), 0)

    def test_garbage_is_none(self):
        self.assertIsNone(data_api.iso_duration(""))
        self.assertIsNone(data_api.iso_duration(None))
        self.assertIsNone(data_api.iso_duration("123"))
        self.assertIsNone(data_api.iso_duration("PTX"))


class TestVideoMeta(unittest.TestCase):
    SETTINGS = {"data_api_enabled": True, "data_api_key": "K-1"}

    def test_normalized_labels_from_data_api(self):
        with mock.patch.object(data_api, "_get",
                               return_value={"items": [api_item()]}) as _get:
            meta = data_api.video_meta("dQw4w9WgXcQ", self.SETTINGS)
        self.assertEqual(meta["duration_s"], 98)
        self.assertEqual(meta["uploaded_at"], "2026-01-02")
        self.assertEqual(meta["view_count"], 1234)
        self.assertEqual(meta["like_count"], 56)
        self.assertEqual(meta["remote_id"], "dQw4w9WgXcQ")
        self.assertEqual(meta["key"], "youtube:dQw4w9WgXcQ")
        self.assertEqual(meta["origin"], "data-api")
        self.assertEqual(meta["thumb_url"], "https://x/h.jpg")
        self.assertEqual(meta["category"], "27")
        self.assertEqual(meta["platform_tags"], ["музыка"])
        self.assertEqual(meta["channel"]["remote_id"], "UCabcd0123456789efghijkl")
        self.assertEqual(meta["channel"]["title"], "Автор")

    def test_labels_match_scrape_normalization(self):
        """Метки Data API == метки обычного normalize_video от yt-dlp info."""
        yt_info = {"id": "dQw4w9WgXcQ", "title": "Название", "duration": 98,
                   "upload_date": "20260102", "view_count": "1234",
                   "like_count": 56, "channel_id": "UCabcd0123456789efghijkl",
                   "channel": "Автор", "description": "описание",
                   # yt-dlp отдаёт category id строкой (27 = "Video from"),
                   # та же метка, что в snippet Data API.
                   "category": "27",
                   "webpage_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}
        scrape_meta = metadata.normalize_video(yt_info, origin="yt-dlp")
        with mock.patch.object(data_api, "_get",
                               return_value={"items": [api_item()]}):
            api_meta = data_api.video_meta("dQw4w9WgXcQ", self.SETTINGS)
        for field in ("remote_id", "key", "title", "description", "duration_s",
                     "uploaded_at", "view_count", "like_count", "category",
                     "webpage_url"):
            self.assertEqual(scrape_meta[field], api_meta[field], field)
        self.assertEqual(scrape_meta["channel"]["remote_id"],
                         api_meta["channel"]["remote_id"])

    def test_empty_items_is_honest_unavailable(self):
        with mock.patch.object(data_api, "_get", return_value={"items": []}):
            with self.assertRaises(FetchError) as caught:
                data_api.video_meta("dQw4w9WgXcQ", self.SETTINGS)
        self.assertEqual(caught.exception.kind, "unavailable")

    def test_without_credentials_honest(self):
        with mock.patch.object(data_api, "_open"):
            with self.assertRaises(FetchError) as caught:
                data_api.video_meta("dQw4w9WgXcQ", {"data_api_enabled": False})
        self.assertEqual(caught.exception.kind, "unsupported")


class TestRequestBuild(unittest.TestCase):
    def _capture(self, settings, calls):
        def fake_open(request):
            calls.append(request)
            return {"items": [api_item()]}
        return mock.patch.object(data_api, "_open", fake_open)

    def test_request_with_api_key(self):
        calls = []
        with self._capture({"data_api_enabled": True, "data_api_key": "K-1"}, calls):
            data_api.video_meta("dQw4w9WgXcQ",
                                {"data_api_enabled": True, "data_api_key": "K-1"})
        url = calls[0].full_url
        self.assertTrue(url.startswith("https://www.googleapis.com/youtube/v3/videos?"))
        self.assertIn("part=snippet%2CcontentDetails%2Cstatistics", url)
        self.assertIn("id=dQw4w9WgXcQ", url)
        self.assertIn("key=K-1", url)
        self.assertNotIn("Authorization", calls[0].headers)

    def test_request_with_oauth_access_token(self):
        calls = []
        settings = {"data_api_enabled": True}
        with mock.patch.object(oauth, "load_tokens",
                              return_value={"broken": False,
                                            "refresh_token": "rt", "scope": "s",
                                            "client_id": "cid", "since": "x"}), \
             mock.patch.object(oauth, "access_token", return_value="AT-9"), \
             self._capture(settings, calls):
            data_api.video_meta("dQw4w9WgXcQ", settings)
        self.assertNotIn("key=", calls[0].full_url)
        self.assertEqual(calls[0].headers["Authorization"], "Bearer AT-9")


class TestHttpErrorMapping(unittest.TestCase):
    def _open_raises(self, exc):
        return mock.patch("urllib.request.urlopen", side_effect=exc)

    def test_quota_kind(self):
        request = urllib.request.Request("https://www.googleapis.com/youtube/v3/videos")
        with self._open_raises(quota_error()):
            with self.assertRaises(FetchError) as caught:
                data_api._open(request)
        self.assertEqual(caught.exception.kind, "quota")
        self.assertIn("Квота", str(caught.exception))

    def test_bad_key_blocked(self):
        request = urllib.request.Request("https://www.googleapis.com/youtube/v3/videos")
        with self._open_raises(http_error(403, '{"error": {"message": "bad key"}}')):
            with self.assertRaises(FetchError) as caught:
                data_api._open(request)
        self.assertEqual(caught.exception.kind, "blocked")

    def test_bad_request_unsupported(self):
        request = urllib.request.Request("https://www.googleapis.com/youtube/v3/videos")
        with self._open_raises(http_error(400, '{"error": {"message": "bad part"}}')):
            with self.assertRaises(FetchError) as caught:
                data_api._open(request)
        self.assertEqual(caught.exception.kind, "unsupported")

    def test_network_failure_blocked(self):
        request = urllib.request.Request("https://www.googleapis.com/youtube/v3/videos")
        with self._open_raises(urllib.error.URLError("connection reset")):
            with self.assertRaises(FetchError) as caught:
                data_api._open(request)
        self.assertEqual(caught.exception.kind, "blocked")
        self.assertIn("не ответила", str(caught.exception))


class TestMetaFallback(unittest.TestCase):
    URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    SCRAPE_INFO = {"id": "dQw4w9WgXcQ", "title": "Название", "duration": 98,
                   "upload_date": "20260102", "view_count": "1234"}

    def test_quota_falls_back_to_scrape(self):
        logs = []
        settings = {"data_api_enabled": True, "data_api_key": "K-1"}
        with mock.patch.object(data_api, "video_meta",
                               side_effect=FetchError("Квота исчерпана", kind="quota")), \
             mock.patch.object(sources, "fetch_video",
                               return_value=dict(self.SCRAPE_INFO)):
            result = data_api.meta(self.URL, settings, log=logs.append)
        self.assertEqual(result["source"], "scrape")
        self.assertEqual(result["api_kind"], "quota")
        self.assertEqual(result["video"]["duration_s"], 98)
        self.assertEqual(result["video"]["uploaded_at"], "2026-01-02")
        self.assertEqual(result["video"]["view_count"], 1234)
        self.assertIn("Data API не доступна, использован скрейп", logs[0])
        self.assertNotIn("\x1b", logs[0])   # журнал без ANSI-кодов

    def test_disabled_uses_scrape_silently(self):
        logs = []
        with mock.patch.object(sources, "fetch_video",
                               return_value=dict(self.SCRAPE_INFO)):
            result = data_api.meta(self.URL, {"data_api_enabled": False},
                                   log=logs.append)
        self.assertEqual(result["source"], "scrape")
        self.assertIsNone(result["api_error"])
        self.assertEqual(logs, [])

    def test_enabled_without_credentials_falls_back_with_hint(self):
        logs = []
        with mock.patch.object(oauth, "load_tokens", return_value=None), \
             mock.patch.object(sources, "fetch_video",
                               return_value=dict(self.SCRAPE_INFO)):
            result = data_api.meta(self.URL, {"data_api_enabled": True},
                                   log=logs.append)
        self.assertEqual(result["source"], "scrape")
        self.assertEqual(result["api_kind"], "unsupported")
        self.assertIn("не настроен", logs[0])

    def test_only_video_links_accepted(self):
        with self.assertRaises(FetchError) as caught:
            data_api.meta("https://www.youtube.com/playlist?list=PLabc123def",
                          {"data_api_enabled": True, "data_api_key": "K"})
        self.assertEqual(caught.exception.kind, "unsupported")
        with self.assertRaises(FetchError):
            data_api.meta("https://www.youtube.com/@handle",
                          {"data_api_enabled": True, "data_api_key": "K"})

    def test_scrape_failure_propagates_honest(self):
        with mock.patch.object(sources, "fetch_video",
                               side_effect=FetchError("Площадка не ответила",
                                                      kind="blocked")):
            with self.assertRaises(FetchError) as caught:
                data_api.meta(self.URL, {"data_api_enabled": False})
        self.assertEqual(caught.exception.kind, "blocked")


class TestDataApiStatus(unittest.TestCase):
    def test_flags(self):
        off = data_api.status({"data_api_enabled": False})
        self.assertFalse(off["enabled"])
        self.assertFalse(off["ready"])
        enabled = data_api.status({"data_api_enabled": True, "data_api_key": "K"})
        self.assertTrue(enabled["ready"])
        self.assertTrue(enabled["key_set"])
        self.assertFalse(enabled["oauth_ready"])
        with mock.patch.object(oauth, "load_tokens",
                               return_value={"broken": False,
                                             "refresh_token": "rt"}):
            oauthed = data_api.status({"data_api_enabled": True})
        self.assertTrue(oauthed["oauth_ready"])
        self.assertTrue(oauthed["ready"])


class TestGuiSurface(GuiCase):
    """Api-входы G2 (additive): аддитивные ответы без UI."""

    def setUp(self):
        super().setUp()
        # Токены OAuth читают профиль через paths - переносим в tmp.
        patcher = mock.patch.dict(os.environ,
                                  {"LOCALAPPDATA": tempfile.mkdtemp(prefix="g2_")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_data_api_meta_validation_and_answer(self):
        api = self.make_api()
        self.assertIn("error", api.data_api_meta({}))
        self.assertIn("error", api.data_api_meta({"url": ""}))
        with mock.patch.object(data_api, "meta",
                               return_value={"ok": True, "source": "scrape",
                                             "video": {"duration_s": 98},
                                             "api_error": None, "api_kind": None}):
            result = api.data_api_meta({"url": self.URL})
        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "scrape")
        with mock.patch.object(data_api, "meta",
                               side_effect=FetchError("не видео", kind="unsupported")):
            result = api.data_api_meta({"url": self.URL})
        self.assertEqual(result["kind"], "unsupported")

    URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    def test_oauth_status_idle_and_flags(self):
        api = self.make_api()
        status = api.oauth_status()
        self.assertEqual(status["phase"], "idle")
        self.assertFalse(status["configured"])
        self.assertFalse(status["signed_in"])
        self.assertIn("client_id", status["hint"])

    def test_oauth_start_requires_client_id(self):
        api = self.make_api()
        result = api.oauth_start({})
        self.assertIn("client_id", result.get("error", ""))

    def test_oauth_forget_clean(self):
        api = self.make_api()
        result = api.oauth_forget()
        self.assertTrue(result["ok"])
        self.assertFalse(result["removed"])
        self.assertEqual(api.oauth_status()["phase"], "idle")

    def test_data_api_status_flags(self):
        api = self.make_api()
        status = api.data_api_status()
        self.assertFalse(status["enabled"])
        self.assertFalse(status["ready"])