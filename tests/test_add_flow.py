"""Флоу добавления источника через Api: фазы A -> B -> C -> D.

Сеть подменяется фикстурой (sources.fetch_snapshot), поэтому тест быстрый
и работает без интернета: важна механика фаз, счётчиков, стадий, отмены и
режимов синхронизации, а не сам YouTube.
"""

import threading
import time
import unittest
from unittest import mock

from app import repo as repo_mod
from app import sources
from app.gui import Api
from tests.test_gui import GuiCase

# ID подобран под ограничения классификатора (10..64 символа [A-Za-z0-9_-]),
# сама ссылка в тестах до сети не доходит.
PLAYLIST_URL = "https://youtube.com/playlist?list=PLfixtur0000000000000000000001"


class _FakeWorker:
    """Заглушка качалки: считает запуски, но ничего не качает."""

    def __init__(self, *args, **kwargs):
        self.started = 0
        self.state = {"running": False, "done": 0, "failed": 0,
                      "attempted": 0, "current": None, "error": None}

    def start(self):
        self.started += 1
        return {"ok": True}

    def stop(self):
        self.state["running"] = False
        return {"ok": True}

    def wait(self, timeout=None):
        return True

    def retry_failed(self):
        return {"ok": True, "retried": 0}


def fixture_snapshot(playlist_id="PLfixtur0000000000000000000001", count=3,
                     channel_id="UCfixtur000000000000000000000001"):
    """Снапшот той же формы, что отдаёт sources.fetch_snapshot."""
    channel = {"platform": "youtube", "remote_id": channel_id,
               "title": "Тестовый автор", "handle": None,
               "url": None, "fallback": False}
    entries = [{
        "position": i, "remote_id": f"vid{i:09d}", "title": f"Видео {i}",
        "duration_s": 60 * i, "uploaded_at": None, "view_count": None,
        "unavailable": False,
        "webpage_url": f"https://youtu.be/vid{i:09d}",
        "channel": channel,
    } for i in range(1, count + 1)]
    return {
        "playlist": {"platform": "youtube", "remote_id": playlist_id,
                     "title": "Тестовый плейлист", "description": None,
                     "kind": "remote", "url": "https://youtube.com/playlist?list=" + playlist_id,
                     "item_count": count, "raw_json": "{}", "channel": channel},
        "entries": entries, "total": count,
        "url": "https://youtube.com/playlist?list=" + playlist_id,
    }


class AddFlowCase(GuiCase):
    def api(self) -> Api:
        return self.make_api()

    def patch_fetch(self, snapshot, delay=0.0):
        def fake(url, settings=None, on_progress=None, stop=None):
            total = snapshot["total"]
            if on_progress:
                on_progress(0, total)
            if delay:
                time.sleep(delay)
            if stop is not None and stop.is_set():
                raise sources.Aborted()
            if on_progress:
                on_progress(total, total)
            return snapshot

        patcher = mock.patch("app.gui.sources.fetch_snapshot", side_effect=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return patcher


class TestPhases(AddFlowCase):
    def test_full_flow_creates_rows_without_touching_db_before_confirm(self):
        api = self.api()
        self.patch_fetch(fixture_snapshot())

        self.assertTrue(api.add_start({"url": PLAYLIST_URL, "mode": "partial"})["ok"])
        flow = self.wait_phase(api, "confirm")
        counts = flow["plan"]["counts"]
        self.assertEqual(counts["new_videos"], 3)
        self.assertEqual(counts["new_channels"], 1)
        # Диалог показывает счётчики, но БД ещё пуста - отмена бесплатна.
        self.assertEqual(api.poll(0)["stats"]["total"], 0)
        self.assertEqual(flow["mode"], "partial")

        self.assertTrue(api.add_confirm()["ok"])
        flow = self.wait_phase(api, "done")
        self.assertEqual(flow["result"]["stats"]["new_videos"], 3)
        self.assertEqual(flow["result"]["mode"], "partial")
        # Частичный режим: очередь пуста, вместо неё - пикер.
        self.assertEqual(flow["result"]["queued"], 0)
        self.assertEqual(flow["result"]["picker_total"], 3)
        self.assertEqual(len(flow["result"]["picker"]), 3)
        self.assertEqual(api.poll(0)["stats"]["total"], 3)
        # Все стадии доведены до done.
        self.assertTrue(all(s["state"] == "done" for s in flow["stages"]))

    def test_stage_progress_is_reported(self):
        api = self.api()
        snapshot = fixture_snapshot(count=6)
        self.patch_fetch(snapshot)
        api.add_start({"url": PLAYLIST_URL})
        self.wait_phase(api, "confirm")
        api.add_confirm()
        flow = self.wait_phase(api, "done")
        names = [s["id"] for s in flow["stages"]]
        self.assertEqual(names, ["playlist", "channels", "videos", "links"])
        videos = [s for s in flow["stages"] if s["id"] == "videos"][0]
        self.assertEqual(videos["total"], 6)

    def test_full_mode_enqueues(self):
        # Качалку подменяем: здесь важен факт «полный режим поставил в
        # очередь и попросил воркер стартовать», а не сама загрузка.
        fake = _FakeWorker()
        with mock.patch("app.gui.DownloadWorker",
                        side_effect=lambda *a, **k: fake):
            api = self.api()
            self.patch_fetch(fixture_snapshot())
            api.add_start({"url": PLAYLIST_URL, "mode": "full"})
            self.wait_phase(api, "confirm")
            api.add_confirm()
            flow = self.wait_phase(api, "done")
        self.assertEqual(flow["result"]["queued"], 3)
        self.assertEqual(api.poll(0)["stats"]["queued"], 3)
        self.assertGreaterEqual(fake.started, 1,
                                "режим «Полная» не запустил качалку")

    def test_manual_mode_has_no_picker(self):
        api = self.api()
        self.patch_fetch(fixture_snapshot())
        api.add_start({"url": PLAYLIST_URL, "mode": "manual"})
        self.wait_phase(api, "confirm")
        api.add_confirm()
        flow = self.wait_phase(api, "done")
        self.assertEqual(flow["result"]["picker_total"], 0)
        self.assertEqual(flow["result"]["queued"], 0)

    def test_readd_is_update_not_duplicate(self):
        api = self.api()
        self.patch_fetch(fixture_snapshot())
        api.add_start({"url": PLAYLIST_URL})
        self.wait_phase(api, "confirm")
        api.add_confirm()
        self.wait_phase(api, "done")
        api.add_close()

        api.add_start({"url": PLAYLIST_URL})
        flow = self.wait_phase(api, "confirm")
        counts = flow["plan"]["counts"]
        self.assertEqual(counts["new_videos"], 0)
        self.assertEqual(counts["known_videos"], 3)
        self.assertTrue(flow["plan"]["exists"])
        api.add_confirm()
        self.wait_phase(api, "done")
        self.assertEqual(api.poll(0)["stats"]["total"], 3)


class TestCancelAndErrors(AddFlowCase):
    def test_cancel_during_fetch_never_writes(self):
        api = self.api()
        self.patch_fetch(fixture_snapshot(), delay=0.5)
        api.add_start({"url": PLAYLIST_URL})
        self.wait_phase(api, "fetching")
        self.assertTrue(api.add_close()["ok"])
        time.sleep(0.8)
        flow = api.poll(0)["add_flow"]
        self.assertIn(flow["phase"], ("idle", "fetching"))
        self.assertEqual(api.poll(0)["stats"]["total"], 0)

    def test_cancel_during_commit_rolls_back(self):
        api = self.api()
        self.patch_fetch(fixture_snapshot())
        api.add_start({"url": PLAYLIST_URL})
        self.wait_phase(api, "confirm")

        # Рвём транзакцию на середине: стадия videos должна откатиться.
        real_commit = repo_mod.commit_plan

        def commit_with_cancel(conn, snapshot, plan, on_stage=None,
                               storage_id=None):
            def hook(name, state, current, total):
                # Рвём на первой записи, дошедшей до стадии видео: у маленького
                # списка промежуточного active может и не быть (сразу done).
                if name == "videos" and current >= 1:
                    api.add_close()          # имитация клика «Отмена»
                if on_stage:
                    on_stage(name, state, current, total)
            return real_commit(conn, snapshot, plan, on_stage=hook,
                               storage_id=storage_id)

        import unittest.mock as mock
        with mock.patch("app.gui.repo.commit_plan", side_effect=commit_with_cancel):
            api.add_confirm()
            deadline = time.time() + 15
            while time.time() < deadline:
                if api.poll(0)["add_flow"]["phase"] == "idle":
                    break
                time.sleep(0.02)
        flow = api.poll(0)["add_flow"]
        self.assertEqual(flow["phase"], "idle", flow.get("error"))
        # Откат: ни плейлиста, ни видео не осталось.
        self.assertEqual(api.poll(0)["stats"]["total"], 0)
        self.assertEqual(api.poll(0)["stats"]["playlists"], 0)

    def test_bad_url_is_rejected_before_network(self):
        api = self.api()
        self.assertEqual(api.add_start({})["error"], "Вставьте ссылку на плейлист или канал")
        res = api.add_start({"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"})
        self.assertIn("плейлист и на канал", res["error"])
        self.assertEqual(api.poll(0)["add_flow"]["phase"], "idle")

    def test_fetch_error_is_shown_not_swallowed(self):
        api = self.api()

        def boom(url, settings=None, on_progress=None, stop=None):
            raise sources.FetchError("Площадка не ответила: 403")

        import unittest.mock as mock
        with mock.patch("app.gui.sources.fetch_snapshot", side_effect=boom):
            api.add_start({"url": PLAYLIST_URL})
            flow = self.wait_phase(api, "error")
        self.assertIn("403", flow["error"])
        self.assertEqual(api.poll(0)["stats"]["total"], 0)

    def test_enqueue_needs_real_ids(self):
        api = self.api()
        self.assertEqual(api.enqueue({"ids": ["не-число"]})["error"],
                         "Некорректный список")
        self.assertEqual(api.enqueue({"ids": []})["queued"], 0)


class TestSourcesUnit(unittest.TestCase):
    """sources без сети: сборка снапшота и наследование автора у канала."""

    def test_collect_inherits_playlist_channel(self):
        # У канала yt-dlp отдаёт записи БЕЗ channel/channel_id.
        playlist_channel = {"platform": "youtube", "remote_id": "UCxxxxxxxx",
                            "title": "Автор", "handle": None, "url": None,
                            "fallback": False}
        raw = [{"id": "aaaaaaaaaaa", "title": "Ролик 1", "duration": 10},
               {"id": "bbbbbbbbbbb", "title": "Ролик 2", "duration": 20},
               None]
        items = sources._collect(raw, playlist_channel, start=1)
        self.assertEqual(len(items), 3)          # None остаётся «дыркой»
        self.assertEqual(items[0]["channel"]["remote_id"], "UCxxxxxxxx")
        self.assertEqual(items[0]["position"], 1)
        self.assertEqual(items[2]["position"], 3)
        self.assertTrue(items[2]["unavailable"])

    def test_collect_dedupes_inside_chunk(self):
        items = sources._collect([{"id": "aaaaaaaaaaa", "title": "A"},
                                  {"id": "aaaaaaaaaaa", "title": "A"}],
                                 None, start=1)
        self.assertEqual(len(items), 1)

    def test_classify_channel_and_playlist(self):
        self.assertEqual(sources.classify("https://www.youtube.com/@vilminessa")["kind"],
                         "channel")
        self.assertEqual(sources.classify("https://youtube.com/playlist?list=PLY5U_JfJ4ZWY")["kind"],
                         "playlist")
        self.assertEqual(sources.classify("просто текст")["kind"], "unknown")


class TestChunking(unittest.TestCase):
    """Сборка чанков без сети: yt-dlp не отдаёт прогресса по записям,
    поэтому большие списки тянутся диапазонами - здесь проверяем арифметику."""

    URL = "https://youtube.com/playlist?list=PLbig0000000000000000000001"

    @staticmethod
    def _head(entries, total):
        return {"_type": "playlist", "id": "PLbig0000000000000000000001",
                "title": "Большой", "playlist_count": total,
                "entries": entries, "channel_id": "UCbig00000000000000000001",
                "channel": "Автор"}

    def test_large_playlist_goes_in_chunks_with_progress(self):
        total = 250
        calls, progress = [], []

        def fake_extract(url, items, settings=None):
            calls.append(items)
            start, end = (int(x) for x in items.split("-"))
            last = min(end, total)
            entries = ([{"id": f"vid{i:09d}", "title": f"Видео {i}"}
                        for i in range(start, last + 1)] if start <= total else [])
            return self._head(entries, total)

        with mock.patch("app.sources._extract", side_effect=fake_extract):
            snapshot = sources.fetch_snapshot(
                self.URL, settings={"delay_ms": 0},
                on_progress=lambda got, want: progress.append((got, want)))

        self.assertEqual(calls, ["1-100", "101-200", "201-300"])
        self.assertEqual(len(snapshot["entries"]), total)
        self.assertEqual(snapshot["playlist"]["item_count"], total)
        # Прогресс растёт и заканчивается ровно на итоге.
        self.assertEqual(progress[0], (100, 250))
        self.assertEqual(progress[-1], (250, 250))
        # Позиции не сбиваются между чанками.
        positions = [e["position"] for e in snapshot["entries"]]
        self.assertEqual(positions, list(range(1, total + 1)))

    def test_stop_between_chunks_aborts(self):
        stop = threading.Event()
        stop.set()   # «Отмена» дошла, пока шёл первый запрос

        def fake_extract(url, items, settings=None):
            return self._head([{"id": "vid000000001", "title": "A"}], 3)

        with mock.patch("app.sources._extract", side_effect=fake_extract):
            with self.assertRaises(sources.Aborted):
                sources.fetch_snapshot(self.URL, stop=stop)

    def test_short_playlist_single_request(self):
        """Маленький список не должен порождать лишних запросов."""
        calls = []

        def fake_extract(url, items, settings=None):
            calls.append(items)
            return self._head([{"id": f"vid{i:09d}", "title": f"V{i}"}
                               for i in range(1, 4)], 3)

        with mock.patch("app.sources._extract", side_effect=fake_extract):
            snapshot = sources.fetch_snapshot(self.URL)
        self.assertEqual(calls, ["1-100"])
        self.assertEqual(len(snapshot["entries"]), 3)


if __name__ == "__main__":
    unittest.main()
