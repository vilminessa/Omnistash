"""PH2: теги площадки (tags/video_tags) - импорт, замена, фильтр.

Теги площадки отделены от локальных пометок пользователя
(videos.user_tags): это разные сущности, разные таблицы и разный UI.
"""

import tempfile
import unittest
from pathlib import Path

from app import db as db_mod
from app import repo
from tests.test_gui import GuiCase


class TagsCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        database = db_mod.Database(Path(self.dir.name) / "t.db")
        self.addCleanup(database.close)
        self.conn = database.conn
        self.vid, _ = repo.upsert_video(self.conn, {
            "platform": "pornhub", "remote_id": "ph12345678",
            "title": "Тест", "channel": None})

    def test_set_and_replace(self):
        repo.set_platform_tags(self.conn, self.vid, "pornhub",
                               ["Anal", "Грубое", "anal"])
        rows = self.conn.execute(
            "SELECT t.slug FROM video_tags vt JOIN tags t "
            "ON t.id=vt.tag_id ORDER BY t.slug").fetchall()
        slugs = [r["slug"] for r in rows]
        # дубль «anal» (разный регистр) схлопнулся в один slug
        self.assertEqual(slugs, ["anal", "грубое"], slugs)
        # полная замена: синк площадки - авторитет
        repo.set_platform_tags(self.conn, self.vid, "pornhub", ["new"])
        rows = self.conn.execute(
            "SELECT t.slug FROM video_tags vt JOIN tags t "
            "ON t.id=vt.tag_id").fetchall()
        self.assertEqual([r["slug"] for r in rows], ["new"])

    def test_platform_tags_flow_through_upsert(self):
        repo.upsert_video(self.conn, {
            "platform": "pornhub", "remote_id": "ph777",
            "title": "X", "channel": None,
            "platform_tags": ["Vintage", "Retro"]}, full=False)
        row = self.conn.execute(
            "SELECT v.id FROM videos v WHERE v.key='pornhub:ph777'"
        ).fetchone()
        count = self.conn.execute(
            "SELECT COUNT(*) n FROM video_tags WHERE video_id=?",
            (row["id"],)).fetchone()["n"]
        self.assertEqual(count, 2)

    def test_none_tags_keep_existing(self):
        repo.set_platform_tags(self.conn, self.vid, "pornhub", ["keep"])
        # source без tags (None) не должен стирать связи
        repo.upsert_video(self.conn, {
            "platform": "pornhub", "remote_id": "ph12345678",
            "title": "Тест", "channel": None,
            "platform_tags": None}, full=False)
        count = self.conn.execute(
            "SELECT COUNT(*) n FROM video_tags WHERE video_id=?",
            (self.vid,)).fetchone()["n"]
        self.assertEqual(count, 1, "None = «в источнике не было», не «пусто»")

    def test_filter_by_tag(self):
        vid2, _ = repo.upsert_video(self.conn, {
            "platform": "pornhub", "remote_id": "ph2", "title": "B",
            "channel": None})
        repo.set_platform_tags(self.conn, self.vid, "pornhub", ["solo"])
        repo.set_platform_tags(self.conn, vid2, "pornhub", ["duo"])
        got = repo.list_videos(self.conn, tag="Solo")
        self.assertEqual(got["total"], 1, "slug нормализуется и в запросе")
        self.assertEqual(got["rows"][0]["title"], "Тест")
        self.assertEqual(repo.list_videos(self.conn, tag="missing")["total"],
                         0)

    def test_tags_popular_counts(self):
        vid2, _ = repo.upsert_video(self.conn, {
            "platform": "pornhub", "remote_id": "ph3", "title": "C",
            "channel": None})
        repo.set_platform_tags(self.conn, self.vid, "pornhub", ["a", "b"])
        repo.set_platform_tags(self.conn, vid2, "pornhub", ["b"])
        popular = repo.tags_popular(self.conn, "pornhub")
        self.assertEqual(popular[0]["slug"], "b")
        self.assertEqual(popular[0]["n"], 2)
        self.assertEqual(len(popular), 2)

    def test_migrated_version_is_10(self):
        path = Path(self.dir.name) / "v10.db"
        db_mod.Database(path).close()
        conn = db_mod.Database(path)
        self.addCleanup(conn.close)
        version = int(conn.conn.execute(
            "PRAGMA user_version").fetchone()[0])
        self.assertEqual(version, 10)
        tables = {r["name"] for r in conn.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("tags", tables)
        self.assertIn("video_tags", tables)


class TestAdultApi(GuiCase):
    """PH3: API вкладки 18+ - платформа-фильтр и чипсы."""

    def test_platform_filter_and_tags_endpoint(self):
        api = self.make_api()
        repo.upsert_video(api.db.conn, {
            "platform": "pornhub", "remote_id": "phX", "title": "PH",
            "channel": None})
        repo.upsert_video(api.db.conn, {
            "platform": "youtube", "remote_id": "ytY", "title": "YT",
            "channel": None})
        got = api.list_videos({"platform": "pornhub"})
        self.assertEqual(got["total"], 1)
        self.assertEqual(got["rows"][0]["title"], "PH")
        vid = got["rows"][0]["id"]
        repo.set_platform_tags(api.db.conn, vid, "pornhub", ["Solo"])
        chips = api.tags_popular({"platform": "pornhub"})
        self.assertTrue(chips["ok"], chips)
        self.assertEqual(chips["tags"][0]["slug"], "solo")
        self.assertEqual(chips["tags"][0]["n"], 1)
        filtered = api.list_videos({"platform": "pornhub", "tag": "solo"})
        self.assertEqual(filtered["total"], 1)


if __name__ == "__main__":
    unittest.main()
