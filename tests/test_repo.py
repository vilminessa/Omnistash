"""План добавления и его принятие: счётчики, стадии, идемпотентность."""

import tempfile
import unittest
from pathlib import Path

from app import repo
from app.db import Database


def entry(remote_id, title, position, channel_id=None, channel_title=None,
          unavailable=False):
    """Плоская запись плейлиста - ровно то, что отдаёт metadata.flat_entry."""
    channel = None
    if channel_id or channel_title:
        channel = {"platform": "youtube", "remote_id": channel_id,
                   "title": channel_title, "handle": None, "url": None,
                   "fallback": not channel_id}
    return {"position": position, "remote_id": remote_id, "title": title,
            "duration_s": 100 + position, "uploaded_at": None,
            "view_count": None, "unavailable": unavailable,
            "webpage_url": f"https://youtu.be/{remote_id}",
            "channel": channel}


def snapshot(entries, playlist_id="PLtest000000000000000000000001", kind="remote"):
    return {
        "playlist": {"platform": "youtube", "remote_id": playlist_id,
                     "title": "Тестовый плейлист", "description": None,
                     "kind": kind, "url": f"https://youtube.com/playlist?list={playlist_id}",
                     "item_count": len(entries), "raw_json": "{}", "channel": None},
        "entries": entries,
    }


class RepoCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "library.db")
        self.conn = self.db.conn

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def counts(self, table):
        return self.conn.execute(f"SELECT COUNT(*) n FROM {table}").fetchone()["n"]


class TestPlanDiff(RepoCase):
    def test_new_playlist(self):
        snap = snapshot([
            entry("vid000000001", "Первое", 1, "UCaaaaaaaaaaaaaaaaaaaaaa", "Автор"),
            entry("vid000000002", "Второе", 2, "UCaaaaaaaaaaaaaaaaaaaaaa", "Автор"),
            entry("vid000000003", "Третье", 3, "UCbbbbbbbbbbbbbbbbbbbbbb", "Второй"),
        ])
        plan = repo.plan_diff(self.conn, snap)
        counts = plan["counts"]
        self.assertEqual(counts["new_videos"], 3)
        self.assertEqual(counts["new_channels"], 2)
        self.assertEqual(counts["links_to_create"], 3)
        self.assertEqual(counts["known_videos"], 0)
        self.assertFalse(plan["playlist"]["exists"])
        # Диалог обязан показывать счётчики, но не оставлять следов.
        self.assertEqual(self.counts("videos"), 0)
        self.assertEqual(self.counts("playlists"), 0)

    def test_unavailable_and_dupes_counted_separately(self):
        snap = snapshot([
            entry("vid000000001", "Ок", 1),
            entry("vid000000001", "Ок же", 2),          # дубль внутри снапшота
            entry(None, "Приватное", 3, unavailable=True),
        ])
        counts = repo.plan_diff(self.conn, snap)["counts"]
        self.assertEqual(counts["new_videos"], 1)
        self.assertEqual(counts["dupes"], 1)
        self.assertEqual(counts["skipped"], 1)

    def test_known_videos_are_only_linked(self):
        snap = snapshot([entry("vid000000001", "Один", 1)])
        repo.commit_plan(self.conn, snap, repo.plan_diff(self.conn, snap))
        plan = repo.plan_diff(self.conn, snap)
        self.assertEqual(plan["counts"]["new_videos"], 0)
        self.assertEqual(plan["counts"]["known_videos"], 1)
        self.assertEqual(plan["counts"]["links_existing"], 1)
        self.assertEqual(plan["counts"]["links_to_create"], 0)
        self.assertTrue(plan["playlist"]["exists"])


class TestCommit(RepoCase):
    def commit(self, snap):
        return repo.commit_plan(self.conn, snap, repo.plan_diff(self.conn, snap))

    def test_creates_in_stage_order(self):
        snap = snapshot([
            entry("vid000000001", "Первое", 1, "UCaaaaaaaaaaaaaaaaaaaaaa", "Автор"),
            entry("vid000000002", "Второе", 2, "UCaaaaaaaaaaaaaaaaaaaaaa", "Автор"),
        ])
        stages = []
        stats = repo.commit_plan(self.conn, snap, repo.plan_diff(self.conn, snap),
                                 on_stage=lambda *a: stages.append(a))
        self.assertEqual(self.counts("playlists"), 1)
        self.assertEqual(self.counts("channels"), 1)
        self.assertEqual(self.counts("videos"), 2)
        self.assertEqual(self.counts("playlist_items"), 2)
        self.assertEqual(stats["new_videos"], 2)
        # Чек-лист видел все четыре стадии и завершил их.
        names = [s[0] for s in stages]
        for stage in ("playlist", "channels", "videos", "links"):
            self.assertIn(stage, names)
        self.assertEqual(stages[-1][1], "done")

    def test_statuses_start_known(self):
        snap = snapshot([entry("vid000000001", "Одно", 1)])
        self.commit(snap)
        rows = repo.list_videos(self.conn)["rows"]
        self.assertEqual(rows[0]["status"], "known")
        self.assertEqual(rows[0]["status_label"], "в индексе")

    def test_positions_and_idempotency(self):
        snap = snapshot([entry(f"vid{i:09d}", f"V{i}", i) for i in range(1, 4)])
        self.commit(snap)
        self.commit(snap)   # повторный запуск после сбоя не плодит дубли
        self.assertEqual(self.counts("videos"), 3)
        self.assertEqual(self.counts("playlist_items"), 3)
        positions = [r["position"] for r in self.conn.execute(
            "SELECT position FROM playlist_items ORDER BY position")]
        self.assertEqual(positions, [1, 2, 3])

    def test_removed_entry_keeps_file_but_marks_item(self):
        self.commit(snapshot([
            entry("vid000000001", "Один", 1),
            entry("vid000000002", "Два", 2),
            entry("vid000000003", "Три", 3),
        ]))
        smaller = snapshot([
            entry("vid000000001", "Один", 1),
            entry("vid000000002", "Два", 2),
        ])
        repo.commit_plan(self.conn, smaller, repo.plan_diff(self.conn, smaller))
        # Пропавшее видео остаётся в библиотеке, связь помечена убывшей.
        self.assertEqual(self.counts("videos"), 3)
        removed = self.conn.execute(
            "SELECT COUNT(*) n FROM playlist_items WHERE removed_at IS NOT NULL"
        ).fetchone()["n"]
        self.assertEqual(removed, 1)

    def test_thin_metadata_does_not_clobber_rich(self):
        snap = snapshot([entry("vid000000001", "Название", 1)])
        self.commit(snap)
        rich = {"title": "Название", "description": "полное описание",
                "raw_json": '{"deep": "значение"}', "remote_id": "vid000000001"}
        repo.upsert_video(self.conn, rich, full=True)
        # Синк приносит тонкий набор из плоской записи - богатый не должен
        # пропасть.
        self.commit(snap)
        row = self.conn.execute(
            "SELECT raw_json, description FROM videos WHERE remote_id='vid000000001'"
        ).fetchone()
        self.assertEqual(row["raw_json"], '{"deep": "значение"}')
        self.assertEqual(row["description"], "полное описание")

    def test_sync_never_touches_local_fields(self):
        snap = snapshot([entry("vid000000001", "Одно", 1)])
        self.commit(snap)
        vid = repo.video_id_by_key(self.conn, "youtube:vid000000001")
        self.conn.execute(
            "UPDATE videos SET user_rating=5, user_tags='избранное', "
            "status='downloaded', notes='своё' WHERE id=?", (vid,))
        self.commit(snap)
        row = self.conn.execute(
            "SELECT user_rating, user_tags, status, notes FROM videos WHERE id=?",
            (vid,)).fetchone()
        self.assertEqual(dict(row), {"user_rating": 5, "user_tags": "избранное",
                                     "status": "downloaded", "notes": "своё"})


class TestRead(RepoCase):
    def setUp(self):
        super().setUp()
        snap = snapshot([
            entry("vid000000001", "Ночной дождик", 1, "UCaaaaaaaaaaaaaaaaaaaaaa", "Автор"),
            entry("vid000000002", "Утренний туман", 2, "UCbbbbbbbbbbbbbbbbbbbbbb", "Второй"),
        ])
        repo.commit_plan(self.conn, snap, repo.plan_diff(self.conn, snap))

    def test_stats_and_tree(self):
        stats = repo.stats(self.conn)
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["playlists"], 1)
        self.assertEqual(stats["channels"], 2)
        tree = repo.tree(self.conn)
        self.assertEqual(len(tree["channels"]), 2)
        self.assertEqual(tree["playlists"][0]["total"], 2)

    def test_search_by_word(self):
        found = repo.list_videos(self.conn, query="дождик")
        self.assertEqual(found["total"], 1)
        self.assertEqual(found["rows"][0]["title"], "Ночной дождик")

    def test_search_with_fts_syntax_chars(self):
        # Двоеточие и звёздочка в запросе - это синтаксис FTS: ломать не должен.
        self.assertEqual(repo.list_videos(self.conn, query="дождик:*")["total"], 1)

    def test_scope_playlist(self):
        playlist_id = self.conn.execute("SELECT id FROM playlists").fetchone()["id"]
        found = repo.list_videos(self.conn, scope={"type": "playlist", "id": playlist_id})
        self.assertEqual(found["total"], 2)

    def test_pagination(self):
        page = repo.list_videos(self.conn, offset=1, limit=1)
        self.assertEqual(page["total"], 2)
        self.assertEqual(len(page["rows"]), 1)

    def test_video_detail(self):
        vid = repo.video_id_by_key(self.conn, "youtube:vid000000001")
        detail = repo.video_detail(self.conn, vid)
        self.assertEqual(detail["title"], "Ночной дождик")
        self.assertEqual(detail["channel"], "Автор")
        self.assertEqual(len(detail["playlists"]), 1)

    def test_queue_guards(self):
        with self.assertRaises(ValueError):
            repo.set_status(self.conn, [1], "нет-такого")
        repo.enqueue(self.conn, [repo.video_id_by_key(self.conn, "youtube:vid000000001")])
        self.assertEqual(repo.stats(self.conn)["queued"], 1)
        # Скачанное в очередь не встаёт.
        vid = repo.video_id_by_key(self.conn, "youtube:vid000000002")
        self.conn.execute("UPDATE videos SET status='downloaded' WHERE id=?", (vid,))
        self.assertEqual(repo.enqueue(self.conn, [vid]), 0)


if __name__ == "__main__":
    unittest.main()
