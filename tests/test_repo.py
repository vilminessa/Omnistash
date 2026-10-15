"""План добавления и его принятие: счётчики, стадии, идемпотентность."""

import json
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

    def test_holes_do_not_mark_removed(self):
        # Дырка (недоступное без remote_id) делает сверку «выпавших»
        # неопределённой: отличить «ролик выпал» от «его не смогли
        # прочитать» нельзя - не гадаем, removed не трогаем.
        self.commit(snapshot([
            entry("vid000000001", "Один", 1),
            entry("vid000000002", "Два", 2),
        ]))
        with_hole = snapshot([
            entry("vid000000001", "Один", 1),
            {"position": 2, "remote_id": None, "title": None,
             "duration_s": None, "view_count": None, "unavailable": True,
             "webpage_url": None, "channel": None},
        ])
        stats = repo.commit_plan(self.conn, with_hole,
                                 repo.plan_diff(self.conn, with_hole))
        self.assertEqual(stats["removed"], 0)
        self.assertEqual(stats["holes"], 1)
        alive = self.conn.execute(
            "SELECT COUNT(*) n FROM playlist_items WHERE removed_at IS NULL"
        ).fetchone()["n"]
        self.assertEqual(alive, 2, "связи не должны помечаться убывшими")

    def test_unavailable_marker_removed_on_sync(self):
        # Строка помечена «удалено», но синк снова принёс её данные:
        # метка снимается - скачанное остаётся «скачанным», без файла -
        # снова «в индексе». Файл и данные не трогаются.
        snap = snapshot([entry("vid000000001", "Вернулось", 1)])
        self.commit(snap)
        vid = self.conn.execute(
            "SELECT id FROM videos WHERE remote_id='vid000000001'"
        ).fetchone()["id"]
        repo.record_file(self.conn, vid, "D:/lib/x.mp4", "video",
                         size=10, mtime=1.0)

        self.conn.execute("UPDATE videos SET status='unavailable' WHERE id=?",
                          (vid,))
        self.commit(snap)
        row = self.conn.execute(
            "SELECT status FROM videos WHERE id=?", (vid,)).fetchone()
        self.assertEqual(row["status"], "downloaded",
                         "скачанное+вернувшееся снова скачано")

        self.conn.execute("UPDATE videos SET status='unavailable' WHERE id=?",
                          (vid,))
        self.conn.execute("DELETE FROM files WHERE video_id=?", (vid,))
        self.commit(snap)
        row = self.conn.execute(
            "SELECT status FROM videos WHERE id=?", (vid,)).fetchone()
        self.assertEqual(row["status"], "known",
                         "без файла - снова просто в индексе")

    def test_sync_stamps_synced_at_and_metrics(self):
        # Полная запись (новый синк) приносит счётчики, дату выхода и
        # обложку; synced_at честно помечает «данные пришли с площадки».
        rich = dict(entry("vid000000001", "Полное", 1))
        rich.update({"uploaded_at": "2021-06-04", "view_count": 100,
                     "like_count": 42, "comment_count": 7,
                     "thumb": "https://i.ytimg.com/x.jpg",
                     "description": "Описание",
                     "raw": json.dumps({"id": "vid000000001",
                                        "like_count": 42})})
        self.commit(snapshot([rich]))
        row = dict(self.conn.execute(
            "SELECT * FROM videos WHERE remote_id='vid000000001'").fetchone())
        self.assertEqual(row["like_count"], 42)
        self.assertEqual(row["comment_count"], 7)
        self.assertEqual(row["uploaded_at"], "2021-06-04")
        self.assertEqual(row["thumb_url"], "https://i.ytimg.com/x.jpg")
        self.assertEqual(row["description"], "Описание")
        self.assertTrue(row["synced_at"], "время синка должно записаться")
        self.assertIn('"like_count": 42', row["raw_json"])

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


class TestListVideosSort(RepoCase):
    """Сортировка списка: белый список колонок и NULLS LAST."""

    def _seed(self):
        # Альфа: дата первая, счётчики средние; Бета: счётчики больше,
        # дата новее; Гамма: вообще без даты и счётчиков (проверка NULLS).
        def add(remote_id, title, date, views, likes, comments, channel,
                duration):
            repo.upsert_video(self.conn, {
                "platform": "youtube", "remote_id": remote_id,
                "key": f"youtube:{remote_id}", "title": title,
                "uploaded_at": date, "duration_s": duration,
                "view_count": views, "like_count": likes,
                "comment_count": comments, "origin": "yt-dlp",
                "webpage_url": f"https://youtu.be/{remote_id}",
                "channel": {"platform": "youtube",
                            "remote_id": "UC" + channel * 22,
                            "title": channel, "handle": None, "url": None},
            })
        add("vid000000001", "альфа", "2023-01-01", 100, 10, 1, "Автор Б", 100)
        add("vid000000002", "Бета", "2024-06-15", 300, 30, 3, "Автор А", 200)
        add("vid000000003", "Гамма", None, None, None, None, "Автор А", 300)

    def titles(self, **kwargs):
        return [row["title"] for row in
                repo.list_videos(self.conn, **kwargs)["rows"]]

    def test_default_is_date_desc(self):
        self._seed()
        self.assertEqual(self.titles(), ["Бета", "альфа", "Гамма"])

    def test_views_direction_and_nulls_last(self):
        self._seed()
        self.assertEqual(
            self.titles(order={"col": "views", "dir": "desc"}),
            ["Бета", "альфа", "Гамма"])
        # По возрастанию пустые всё равно в конце.
        self.assertEqual(
            self.titles(order={"col": "views", "dir": "asc"}),
            ["альфа", "Бета", "Гамма"])

    def test_metrics_columns_sort(self):
        self._seed()
        self.assertEqual(self.titles(order={"col": "likes", "dir": "desc"})[:2],
                         ["Бета", "альфа"])
        self.assertEqual(self.titles(order={"col": "comments", "dir": "asc"})[:2],
                         ["альфа", "Бета"])


    def test_title_is_case_insensitive(self):
        self._seed()
        # Русская локаль без учёта регистра: «альфа» < «Бета» < «Гамма».
        self.assertEqual(self.titles(order={"col": "title", "dir": "asc"}),
                         ["альфа", "Бета", "Гамма"])

    def test_channel_and_duration_and_status(self):
        self._seed()
        first = repo.list_videos(self.conn, order={"col": "channel", "dir": "asc"},
                                 limit=1)["rows"][0]
        self.assertEqual(first["channel"], "Автор А")   # А < Б, NULLS нет
        self.assertEqual(
            self.titles(order={"col": "duration", "dir": "asc"})[0], "альфа")
        self.assertEqual(
            self.titles(order={"col": "status", "dir": "asc"})[0] is not None, True)

    def test_size_uses_computed_column(self):
        self._seed()
        # Размер - подзапрос по files: даём «Бете» файл.
        vid = self.conn.execute(
            "SELECT id FROM videos WHERE title='Бета'").fetchone()["id"]
        repo.record_file(self.conn, vid, "D:/lib/Бета.mp4", "video",
                         size=500, mtime=1.0)
        rows = repo.list_videos(self.conn, order={"col": "size", "dir": "desc"})["rows"]
        self.assertEqual(rows[0]["title"], "Бета")
        self.assertEqual(rows[0]["size"], 500)

    def test_unknown_column_falls_back_to_default(self):
        self._seed()
        # Ключ из запроса окна не является SQL: мусор молча отвергается,
        # порядок остаётся дефолтным (дата ↓).
        hacked = self.titles(order={"col": "title; DROP TABLE videos", "dir": "asc"})
        self.assertEqual(hacked, ["Бета", "альфа", "Гамма"])
        self.assertEqual(self.counts("videos"), 3, "таблица должна выжить")

    def test_sync_column_sorts(self):
        self._seed()
        self.conn.execute(
            "UPDATE videos SET synced_at='2026-01-01T00:00:00' "
            "WHERE title='Гамма'")
        rows = repo.list_videos(self.conn, order={"col": "sync", "dir": "desc"})["rows"]
        self.assertEqual(rows[0]["title"], "Гамма")



class TestAdultExclude(RepoCase):
    """P2: взрослые площадки скрыты из ОБЩЕГО списка (только своей вкладкой)."""

    def _seed(self):
        repo.upsert_video(self.conn, {
            "platform": "youtube", "remote_id": "vid000000001",
            "title": "Обычное", "channel": None})
        repo.upsert_video(self.conn, {
            "platform": "pornhub", "remote_id": "ph00000001",
            "title": "Возрастное", "channel": None})
        repo.upsert_video(self.conn, {
            "platform": "pornhub", "remote_id": "ph00000002",
            "title": "Ещё возрастное", "channel": None})

    def titles(self, **kwargs):
        return [row["title"] for row in
                repo.list_videos(self.conn, **kwargs)["rows"]]

    def test_pool_hides_adult(self):
        self._seed()
        self.assertEqual(self.titles(exclude_adult=True), ["Обычное"])
        # без флага (старое поведение) - всё видно
        self.assertEqual(len(self.titles()), 3)

    def test_platform_view_shows_only_hers(self):
        self._seed()
        adult = self.titles(platform="pornhub")
        self.assertEqual(len(adult), 2)
        self.assertNotIn("Обычное", adult)
        # явный platform сильнее exclude (вкладка площадки работает)
        self.assertEqual(
            len(self.titles(platform="pornhub", exclude_adult=True)), 2)

    def test_tag_filter_stacks_with_exclude(self):
        """P3: теговый фильтр библиотеки стыкуется со скрытием взрослого."""
        self._seed()
        vid = self.conn.execute(
            "SELECT id FROM videos WHERE title='Обычное'").fetchone()["id"]
        repo.set_platform_tags(self.conn, vid, "youtube", ["Музыка"])
        got = repo.list_videos(self.conn, exclude_adult=True, tag="музыка")
        self.assertEqual([r["title"] for r in got["rows"]], ["Обычное"])
        # взрослого с тем же тегом в общем фильтре нет
        ph = self.conn.execute(
            "SELECT id FROM videos WHERE platform='pornhub' LIMIT 1"
        ).fetchone()["id"]
        repo.set_platform_tags(self.conn, ph, "pornhub", ["Музыка"])
        got = repo.list_videos(self.conn, exclude_adult=True, tag="музыка")
        self.assertEqual(got["total"], 1)

if __name__ == "__main__":
    unittest.main()
