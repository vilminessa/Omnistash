"""PO/visitor-токены площадки «не бот» (M13): профиль, повтор, честный итог.

Офлайн: сеть подменяется фикстурой YoutubeDL, профиль - временный каталог.
Токены читаются и пишутся тем же шифрованием DPAPI, что куки аккаунта
(google_auth.encrypt/decrypt). Обход DPI здесь отсутствует: ссылка, домен и
сеть не меняются, кроме зеркала домена реестра.

Проверяемое поведение:
  * токен и visitor-info сохраняются в профиле и читаются обратно;
  * capture_token берёт из cookies.txt только подтверждающие cookie;
  * повтор выкачки с visitor-info / PO-токеном / cookie подтверждения;
  * токен из настройки сохраняется в профиле сам;
  * подтверждение протухло -> дороги его не используют;
  * «не помогло» -> kind=bot и подсказка, без бесконечных повторов.
"""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from app import bypass, downloader, google_auth, paths, sources
from yt_dlp.utils import DownloadError

BOT_TEXT = ("ERROR: [youtube] Unable to extract information, "
            "please sign in to confirm you're not a bot")
COOKIE_FILE = ("youtube.com\tTRUE\t/\tFALSE\t0\tVISITOR_INFO1_LIVE\tvisitor-abc\n"
               "youtube.com\tTRUE\t/\tFALSE\t0\tSOCS\tagree\n"
               "youtube.com\tTRUE\t/\tFALSE\t0\tSECRET_SESSION\tnever-kept\n")


class TokenCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.profile = Path(self._tmp.name)
        patcher = mock.patch.object(paths, "profile_dir", lambda: self.profile)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_ctx(self, settings=None, url="https://example.com/v1", **kw):
        """ctx без куки-дорог и имитации: бот-дороги проверяются отдельно."""
        return bypass.make_context(settings or {}, {}, url,
                                   platform="youtube", purpose="meta",
                                   browsers=[], target_of=lambda s: None, **kw)


class TestTokenStore(TokenCase):
    def test_save_and_read_roundtrip(self):
        result = bypass.save_token("youtube", po_token="web.gvs+abcdef0123456789",
                                   visitor="visitor-abc")
        self.assertTrue(result["ok"], result)
        record = bypass.token_record("youtube")
        self.assertEqual(record["po_token"], "web.gvs+abcdef0123456789")
        self.assertEqual(record["visitor_data"], "visitor-abc")
        self.assertEqual(record["capability"], "po")
        self.assertIn("saved_at", record)

    def test_store_is_encrypted_on_disk(self):
        bypass.save_token("youtube", visitor="visitor-abc")
        raw = bypass.token_path().read_bytes()
        self.assertNotIn(b"visitor-abc", raw,
                         "в профиле лежит шифр, а не значение токена")

    def test_forget_removes_only_platform_record(self):
        bypass.save_token("youtube", visitor="visitor-abc")
        bypass.save_token("pornhub", cookies=["pornhub.com\tTRUE\t/\tFALSE\t0\tkwk_age_verified\tyes"])
        self.assertTrue(bypass.forget_token("youtube")["removed"])
        self.assertIsNone(bypass.token_record("youtube"))
        self.assertIsNotNone(bypass.token_record("pornhub"),
                             "запись прочих площадок цела")

    def test_expired_record_is_none(self):
        bypass.save_token("youtube", visitor="visitor-abc")
        with mock.patch.object(bypass, "TOKEN_TTL", 0):
            self.assertIsNone(bypass.token_record("youtube"),
                              "протухший токен - честное «токена нет»")

    def test_capture_from_cookie_file(self):
        path = self.profile / "cookies.txt"
        path.write_text(COOKIE_FILE, encoding="utf-8")
        result = bypass.capture_token("youtube", path)
        self.assertTrue(result["ok"], result)
        record = bypass.token_record("youtube")
        self.assertEqual(record["visitor_data"], "visitor-abc")
        self.assertNotIn("SECRET_SESSION", "".join(record["cookies"]),
                         "прочие cookie в профиль не кладём")

    def test_capture_rejects_file_without_confirmation(self):
        result = bypass.capture_token("youtube", "example.com\tTRUE\t/\tFALSE\t0\tx\ty")
        self.assertFalse(result["ok"])
        self.assertIn("нет подтверждения", result["error"])

    def test_cookiefile_from_record_filters_and_cleans(self):
        bypass.save_token("youtube", cookies=[
            "youtube.com\tTRUE\t/\tFALSE\t0\tVISITOR_INFO1_LIVE\tvisitor-abc",
            "youtube.com\tTRUE\t/\tFALSE\t0\tSECRET_SESSION\tnever-kept"])
        path = bypass.cookiefile_from_record(bypass.token_record("youtube"),
                                             platform="youtube")
        self.assertIsNotNone(path)
        text = Path(path).read_text(encoding="utf-8")
        self.assertIn("VISITOR_INFO1_LIVE", text)
        self.assertNotIn("SECRET_SESSION", text)
        ctx = {"temp": [path]}
        bypass.cleanup(ctx)
        self.assertFalse(Path(path).exists(), "временный файл дороги убирается")

    def test_po_overlay_shapes(self):
        self.assertEqual(bypass.po_overlay("abcdef0123456789"),
                         {"extractor_args": {"youtube":
                                             {"po_token": ["web.gvs+abcdef0123456789"]}}})
        self.assertEqual(bypass.po_overlay("web.gvs+abcdef0123456789"),
                         {"extractor_args": {"youtube":
                                             {"po_token": ["web.gvs+abcdef0123456789"]}}})
        self.assertIsNone(bypass.po_overlay("корот"),
                          "слишком короткий токен - не мусор, а «не применимо»")
        self.assertIsNone(bypass.po_overlay(""))
        self.assertEqual(bypass.visitor_overlay("visitor-abc"),
                         {"extractor_args": {"youtube":
                                             {"visitor_data": ["visitor-abc"]}}})
        self.assertIsNone(bypass.visitor_overlay("   "))

    def test_merge_overlay_keeps_existing_args(self):
        merged = bypass.merge_overlay({"format": "best"},
                                      {"extractor_args": {"youtube":
                                                          {"po_token": ["a"]}}})
        merged = bypass.merge_overlay(merged, {"extractor_args":
                                               {"youtube": {"visitor_data": ["b"]}}})
        self.assertEqual(merged["format"], "best", "прочие опции не затираются")
        self.assertEqual(merged["extractor_args"]["youtube"],
                         {"po_token": ["a"], "visitor_data": ["b"]})


class TestRerunWithToken(TokenCase):
    def test_visitor_token_wins_the_rerun(self):
        bypass.save_token("youtube", visitor="visitor-abc")
        captured: list = []

        def attempt(o, u):
            captured.append(dict(o))
            if not o.get("extractor_args"):
                raise DownloadError(BOT_TEXT)
            return {"id": "v1"}

        outcome = bypass.run_chain(attempt, self.make_ctx(),
                                   classify=sources.classify_failure)
        self.assertTrue(outcome["ok"], outcome)
        self.assertEqual(outcome["winner"], "visitor")
        self.assertEqual(captured[-1]["extractor_args"]["youtube"],
                         {"visitor_data": ["visitor-abc"]})

    def test_po_token_from_setting_saved_in_profile(self):
        captured: list = []

        def attempt(o, u):
            captured.append(dict(o))
            if not o.get("extractor_args"):
                raise DownloadError(BOT_TEXT)
            return {"id": "v1"}

        ctx = self.make_ctx(settings={"bot_token": "web.gvs+abcdef0123456789"})
        outcome = bypass.run_chain(attempt, ctx,
                                   classify=sources.classify_failure)
        self.assertTrue(outcome["ok"], outcome)
        self.assertEqual(outcome["winner"], "po_token")
        self.assertEqual(captured[-1]["extractor_args"]["youtube"]["po_token"],
                         ["web.gvs+abcdef0123456789"])
        record = bypass.token_record("youtube")
        self.assertEqual(record["po_token"], "web.gvs+abcdef0123456789",
                         "токен из настройки сохранён в профиле (DPAPI)")

    def test_confirm_cookie_for_age_platform(self):
        bypass.save_token("youtube", cookies=[
            "youtube.com\tTRUE\t/\tFALSE\t0\tSOCS\tagree"])
        captured: list = []

        def attempt(o, u):
            captured.append(dict(o))
            if not o.get("cookiefile"):
                raise DownloadError(BOT_TEXT)
            return {"id": "v1"}

        ctx = self.make_ctx()
        outcome = bypass.run_chain(attempt, ctx,
                                   classify=sources.classify_failure)
        self.assertTrue(outcome["ok"], outcome)
        self.assertEqual(outcome["winner"], "confirm_cookie")
        self.assertTrue(ctx["temp"], "временный cookies.txt создаётся под дорогу")
        text = Path(ctx["temp"][0]).read_text(encoding="utf-8")
        self.assertIn("SOCS", text)
        bypass.cleanup(ctx)
        self.assertFalse(Path(ctx["temp"][0]).exists(),
                         "файл дороги живёт один прогон и убирается")

    def test_not_helped_is_bot_with_hint_and_no_infinite_repeats(self):
        bypass.save_token("youtube", po_token="web.gvs+abcdef0123456789",
                          visitor="visitor-abc",
                          cookies=["youtube.com\tTRUE\t/\tFALSE\t0\tSOCS\tagree"])
        captured: list = []

        def attempt(o, u):
            captured.append(dict(o))
            raise DownloadError(BOT_TEXT)

        ctx = self.make_ctx()
        outcome = bypass.run_chain(attempt, ctx, classify=sources.classify_failure,
                                   humanize=lambda exc, kind: sources.failure_message(
                                       kind or "network", str(exc)))
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["kind"], "bot")
        self.assertEqual(len(captured), 4,
                         "ровно 4 дорог (дефолт): plain, visitor, po, confirm")
        self.assertIn("дороги выкачки кончились", outcome["hint"])
        self.assertIn("не бот", outcome["error"])
        self.assertIn("участников сети", outcome["error"],
                      "mesh подсказка: запросить файл у участников")

    def test_bot_disabled_means_no_token_roads(self):
        bypass.save_token("youtube", po_token="web.gvs+abcdef0123456789")
        captured: list = []

        def attempt(o, u):
            captured.append(dict(o))
            raise DownloadError(BOT_TEXT)

        ctx = self.make_ctx(settings={"bot_token_enabled": False})
        outcome = bypass.run_chain(attempt, ctx, classify=sources.classify_failure,
                                   humanize=lambda exc, kind: sources.failure_message(
                                       kind or "network", str(exc)))
        self.assertFalse(outcome["ok"])
        self.assertEqual(len(captured), 1,
                         "выключено - бот-дороги не включаются даже с токеном")
        self.assertNotIn("extractor_args", captured[0])

    def test_expired_token_not_reused(self):
        bypass.save_token("youtube", visitor="visitor-abc")
        captured: list = []

        def attempt(o, u):
            captured.append(dict(o))
            raise DownloadError(BOT_TEXT)

        with mock.patch.object(bypass, "TOKEN_TTL", 0):
            ctx = self.make_ctx({"bypass_max_attempts": 12})
            outcome = bypass.run_chain(attempt, ctx,
                                       classify=sources.classify_failure)
        self.assertFalse(outcome["ok"])
        self.assertNotIn("extractor_args", captured[0])
        self.assertEqual(outcome["tally"]["fail"], 1,
                         "протухший токен - дороги мимо, не бесконечный повтор")

    def test_same_token_not_repeated_twice(self):
        """Повтор тем же visitor-info бессмыслен: дорога одна, не две."""
        bypass.save_token("youtube", visitor="visitor-abc")
        captured: list = []

        def attempt(o, u):
            captured.append(dict(o))
            raise DownloadError(BOT_TEXT)

        ctx = self.make_ctx({"bypass_max_attempts": 12})
        ctx["opts"] = {"extractor_args": {"youtube": {"visitor_data":
                                                      ["visitor-abc"]}}}
        outcome = bypass.run_chain(attempt, ctx, classify=sources.classify_failure)
        self.assertFalse(outcome["ok"])
        self.assertEqual([r["route"] for r in outcome["routes"]], ["plain"],
                         "visitor уже подключён - повторной дороги нет")


class TestSourcesFetchWithErrorKind(TokenCase):
    """fetch_video/list: kind бот доходит до FetchError и до UI (паттерн
    test_add_errors.py)."""

    def setUp(self):
        super().setUp()
        self._browsers = mock.patch.object(google_auth, "BROWSER_CHOICES", ())
        self._browsers.start()
        self.addCleanup(self._browsers.stop)
        self._imp = mock.patch.object(downloader, "impersonate_target",
                                      lambda s: None)
        self._imp.start()
        self.addCleanup(self._imp.stop)

    def test_bot_kind_from_fetch_video(self):
        plan = ["errbot", "errbot"]      # plain и mirror - обе падают

        class Y:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, url, download=False):
                action = plan.pop(0) if plan else "ok"
                if action == "errbot":
                    raise DownloadError(BOT_TEXT)
                return {"id": "v1"}

        with mock.patch.object(sources.yt_dlp, "YoutubeDL",
                               side_effect=lambda opts: Y()):
            with self.assertRaises(sources.FetchError) as ctx_exc:
                sources.fetch_video("https://youtu.be/vid0000000001",
                                    settings={"retries": 1})
        self.assertEqual(ctx_exc.exception.kind, "bot")
        self.assertIn("не бот", str(ctx_exc.exception))
        self.assertIn("участников сети", str(ctx_exc.exception))
        self.assertEqual(plan, [], "дороги отработали обе две доступные")

    def test_token_wins_the_road_after_mirror(self):
        """Порядок честный: зеркало домена, затем visitor-info из профиля."""
        bypass.save_token("youtube", visitor="visitor-abc")
        captured: list = []

        class Y:
            def __init__(self, opts):
                captured.append(dict(opts))

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, url, download=False):
                if not captured[-1].get("extractor_args"):
                    raise DownloadError(BOT_TEXT)
                return {"id": "v1"}

        with mock.patch.object(sources.yt_dlp, "YoutubeDL", side_effect=Y):
            info = sources.fetch_video("https://youtu.be/vid0000000001",
                                       settings={"retries": 1})
        self.assertEqual(info["id"], "v1")
        self.assertEqual(len(captured), 3, "plain -> mirror -> visitor")
        self.assertEqual(captured[-1]["extractor_args"]["youtube"],
                         {"visitor_data": ["visitor-abc"]})


class TestDownloaderRerunWithToken(TokenCase):
    """Файловая выкачка: та же цепочка с токеном через downloader._download."""

    def test_download_retry_with_visitor_token(self):
        dest = self.profile / "dl"
        dest.mkdir()
        bypass.save_token("youtube", visitor="visitor-abc")
        captured: list = []

        class FakeYDL:
            def __init__(self, opts):
                captured.append(dict(opts))

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def add_post_processor(self, *a, **kw):
                pass

            def extract_info(self, url, download=True):
                if not captured[-1].get("extractor_args"):
                    raise DownloadError(BOT_TEXT)
                return {"id": "v1", "title": "Т", "ext": "mp4",
                        "requested_downloads": []}

        patchers = [
            mock.patch.object(downloader.yt_dlp, "YoutubeDL", side_effect=FakeYDL),
            mock.patch.object(downloader, "impersonate_target", lambda s: None),
            mock.patch.object(google_auth, "BROWSER_CHOICES", ()),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        result = downloader._download(
            {"webpage_url": "https://youtu.be/vid0000000001", "id": "v1",
             "platform": "youtube"},
            {"retries": 1, "dest_dir": str(dest)},
            stop=threading.Event())
        self.assertIsNone(result["error"], result)
        self.assertEqual(captured[-1]["extractor_args"]["youtube"],
                         {"visitor_data": ["visitor-abc"]})


if __name__ == "__main__":
    unittest.main()
