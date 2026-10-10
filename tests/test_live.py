"""Живые проверки на настоящем YouTube.

Пропускаются по умолчанию, чтобы обычный прогон не зависел от сети:

    OMNISTASH_LIVE=1 python -m unittest tests.test_live -v

URL-ы - те, что дал автор проекта: тестовый плейлист «test» и собственный
канал. Профиль (настройки и база) переносится во временную папку, настоящая
библиотека при этом не трогается.
"""

import os
import time
import unittest

from app import repo
from tests.test_gui import GuiCase

LIVE = os.environ.get("OMNISTASH_LIVE") == "1"

PLAYLIST_URL = "https://youtube.com/playlist?list=PLY5U_JfJ4ZWY&si=NtUrtq8za0Iawwed"
CHANNEL_URL = "https://www.youtube.com/@vilminessa"


@unittest.skipUnless(LIVE, "живая сеть: запуск с OMNISTASH_LIVE=1")
class LiveCase(GuiCase):
    def wait(self, api, phase, timeout=90.0):
        """Дождаться фазы; неожиданная ошибка - тоже провал с её текстом."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            flow = api.poll(0)["add_flow"]
            if flow["phase"] == phase:
                return flow
            if flow["phase"] == "error":
                self.fail(f"ошибка вместо «{phase}»: {flow.get('error')}")
            time.sleep(0.05)
        self.fail(f"фаза {phase} не наступила, сейчас "
                  f"{api.poll(0)['add_flow']['phase']}")


class TestLivePlaylist(LiveCase):
    def test_add_then_readd_shows_only_new(self):
        api = self.make_api()

        # --- фаза A: индексация ---
        self.assertTrue(api.add_start({"url": PLAYLIST_URL, "mode": "manual"})["ok"])
        flow = self.wait(api, "confirm")
        counts = flow["plan"]["counts"]
        self.assertGreaterEqual(counts["new_videos"], 1, counts)
        self.assertEqual(counts["known_videos"], 0)
        self.assertFalse(flow["plan"]["exists"])
        # Диалог с планом ничего не записал: БД пуста.
        self.assertEqual(api.poll(0)["stats"]["total"], 0)

        # --- фаза C: создание ---
        self.assertTrue(api.add_confirm()["ok"])
        flow = self.wait(api, "done")
        self.assertEqual(flow["result"]["stats"]["new_videos"], counts["new_videos"])
        self.assertTrue(all(s["state"] == "done" for s in flow["stages"]))
        # Режим «Ручная»: ни очереди, ни пикера.
        self.assertEqual(flow["result"]["queued"], 0)
        self.assertEqual(flow["result"]["picker_total"], 0)

        stats = api.poll(0)["stats"]
        self.assertEqual(stats["total"], counts["new_videos"])
        self.assertEqual(stats["playlists"], 1)

        tree = api.poll(0)["tree"]
        self.assertTrue(tree["playlists"], "плейлист не появился в дереве")
        self.assertEqual(tree["playlists"][0]["total"], counts["new_videos"])

        # --- повторное добавление: только обновление ---
        api.add_close()
        self.assertTrue(api.add_start({"url": PLAYLIST_URL})["ok"])
        flow = self.wait(api, "confirm")
        again = flow["plan"]["counts"]
        self.assertEqual(again["new_videos"], 0)
        self.assertGreaterEqual(again["known_videos"], 1)
        self.assertTrue(flow["plan"]["exists"])
        api.add_confirm()
        self.wait(api, "done")
        # Дублей не появилось.
        self.assertEqual(api.poll(0)["stats"]["total"], stats["total"])


class TestLiveChannel(LiveCase):
    def test_channel_entries_inherit_author(self):
        api = self.make_api()

        self.assertTrue(api.add_start({"url": CHANNEL_URL, "mode": "manual"})["ok"])
        flow = self.wait(api, "confirm")
        # Канал - это плейлист загрузок.
        self.assertEqual(flow["plan"]["kind"], "uploads")
        self.assertGreaterEqual(flow["plan"]["counts"]["new_videos"], 1)

        api.add_confirm()
        flow = self.wait(api, "done")
        created = flow["result"]["stats"]["new_videos"]

        tree = api.poll(0)["tree"]
        self.assertTrue(tree["channels"], "канал не попал в дерево «Каналы»")
        # У канальных записей channel/channel_id в ответе площадки НЕТ -
        # без наследования автора эти видео висели бы «без автора».
        self.assertEqual(tree["channels"][0]["total"], created)

        rows = repo.list_videos(api.db.conn, limit=100)["rows"]
        self.assertEqual(len(rows), created)
        for row in rows:
            self.assertEqual(row["channel"], tree["channels"][0]["title"],
                             f"видео {row['title']!r} потеряло автора")

        # Статус у всех «в индексе»: ничего не качалось.
        self.assertTrue(all(r["status"] == "known" for r in rows))


class TestLiveDownload(LiveCase):
    def test_download_then_reindex_recognises_file(self):
        """Сквозная: очередь -> mp4 (обложка/метаданные внутри) ->
        .omnistash.json на папку -> свежий скан опознаёт файл."""
        import pathlib

        from app import indexer
        from app.metadata import video_key

        api = self.make_api()
        dest = self.dir / "downloads"
        dest.mkdir(parents=True, exist_ok=True)
        # Куда качать - выбор человека: в тесте это хранилище по умолчанию.
        storage = self.add_storage(api, dest)
        api.save_setting({"key": "default_storage_id", "value": storage["id"]})
        api.save_setting({"key": "subtitles", "value": "none"})

        # 1. заносим канал в индекс
        self.assertTrue(api.add_start({"url": CHANNEL_URL, "mode": "manual"})["ok"])
        self.wait(api, "confirm")
        api.add_confirm()
        self.wait(api, "done")

        # 2. берём самое короткое видео канала
        row = api.db.conn.execute(
            """SELECT id, key, duration_s FROM videos
                WHERE duration_s IS NOT NULL ORDER BY duration_s LIMIT 1"""
        ).fetchone()
        self.assertIsNotNone(row, "в канале нет видео с известной длительностью")
        self.assertEqual(api.enqueue({"ids": [row["id"]]})["queued"], 1)

        # 3. качаем (реальная сеть, поэтому щедрый таймаут)
        self.assertTrue(api.dl.wait(timeout=240), "воркер не завершился")
        state = api.dl.state
        self.assertEqual(state["failed"], 0, state)

        status = api.db.conn.execute(
            "SELECT status FROM videos WHERE id=?", (row["id"],)).fetchone()["status"]
        self.assertEqual(status, "downloaded")

        # 4. файл, агрегат папки и хеш на месте, имя - по шаблону с ID.
        #    Режим по умолчанию - «всё в видео»: пофайловых сайдкаров нет.
        files = [dict(r) for r in api.db.conn.execute(
            "SELECT kind, path, size, hash FROM files WHERE video_id=?",
            (row["id"],))]
        kinds = {f["kind"] for f in files}
        self.assertIn("video", kinds)
        self.assertNotIn("sidecar", kinds,
                         "embed-режим не должен писать post.json")
        video_file = next(f for f in files if f["kind"] == "video")
        path = pathlib.Path(video_file["path"])
        self.assertTrue(path.is_file(), path)
        self.assertTrue(video_file["hash"], "хеш файла не записан")
        remote_id = row["key"].split(":", 1)[1]
        self.assertIn(f"[{remote_id}]", path.name,
                      f"имя не по шаблону: {path.name}")

        from app import aggregate
        record = aggregate.find(path.parent, remote_id)
        self.assertIsNotNone(record, "запись агрегата не написана")
        self.assertEqual(record["path"], path.name)
        self.assertEqual(record["hash"], video_file["hash"],
                         "в записи - хеш финального (в том числе со вшитой "
                         "обложкой) файла")

        # С ffmpeg обложка и метаданные вшиваются: рядом с видео ничего,
        # а внутри файла лежат теги. Без ffmpeg встроить нечем - обложка
        # честно остаётся файлом, очередь об этом предупреждает.
        from app import downloader
        ffmpeg = downloader.find_ffmpeg()
        if ffmpeg:
            self.assertNotIn("thumbnail", kinds,
                             "обложка должна быть вшита, а не лежать рядом")
            probe = pathlib.Path(ffmpeg).with_name("ffprobe.exe")
            if probe.is_file():
                import json as _json
                import subprocess
                done = subprocess.run(
                    [str(probe), "-v", "error", "-show_entries",
                     "format_tags=title", "-of", "json", str(path)],
                    capture_output=True, text=True, timeout=60)
                tags = ((_json.loads(done.stdout or "{}").get("format")
                         or {}).get("tags") or {})
                self.assertIn("title", tags, "метаданные не вшиты в файл")
        else:
            self.assertIn("thumbnail", kinds,
                          "без ffmpeg обложка должна остаться файлом")
        self.assertEqual(api.poll(0)["stats"]["downloaded"], 1)

        # 5. переезд библиотеки: СВЕЖАЯ база (путь она не знает) обязана
        #    опознать файл по агрегату (шаг 2) или по ID в имени (шаг 3),
        #    а не завести «неизвестный».
        from app import storages as storages_mod
        from app.db import Database as FreshDatabase

        fresh = FreshDatabase(self.dir / "library_move.db")
        try:
            # Как это делает человек: сначала добавляет папку, потом скан.
            fresh_storage = storages_mod.add(fresh.conn, str(dest))
            self.assertTrue(fresh_storage.get("id"), fresh_storage)
            report = indexer.scan([fresh_storage], fresh, compute_hash=True)
            self.assertGreaterEqual(report["bound_sidecar"] + report["bound_id"],
                                    1, report)
            self.assertEqual(report["added"], 0,
                             "файл опознан не был и стал «неизвестным»")
            self.assertEqual(report["missing"], 0, report)

            moved = fresh.conn.execute(
                "SELECT key, status, title FROM videos").fetchall()
            self.assertEqual(len(moved), 1, [dict(r) for r in moved])
            self.assertEqual(moved[0]["key"], row["key"])
            self.assertEqual(moved[0]["status"], "downloaded")
            # Путь записан канонически: хранилище + относительный.
            bound = fresh.conn.execute(
                "SELECT storage_id, rel_path FROM files WHERE kind='video'"
            ).fetchone()
            self.assertEqual(bound["storage_id"], fresh_storage["id"])
            self.assertTrue(bound["rel_path"], bound["rel_path"])
        finally:
            fresh.close()

        # А повторный скан в той же базе ничего не меняет (fast-path).
        again = indexer.scan([storage], api.db, compute_hash=True)
        self.assertEqual(again["added"] + again["rebound"], 0, again)
        self.assertEqual(again["missing"], 0, again)


class TestLiveSync(LiveCase):
    def test_sync_over_both_sources_finds_nothing_new(self):
        """Реальный переснапшот: плейлист и канал уже занесены -> 0 новых."""
        api = self.make_api()
        for url in (PLAYLIST_URL, CHANNEL_URL):
            self.assertTrue(api.add_start({"url": url, "mode": "manual"})["ok"])
            self.wait(api, "confirm")
            api.add_confirm()
            self.wait(api, "done")
            api.add_close()

        total_before = api.poll(0)["stats"]["total"]
        self.assertGreater(total_before, 0)

        self.assertTrue(api.sync_start().get("ok"))
        sync = self.wait_sync(api)
        self.assertEqual(len(sync["results"]), 2, sync)
        for row in sync["results"]:
            self.assertIsNone(row["error"], row)
            self.assertEqual(row["new"], 0,
                             f"площадка отдала новое: {row}")
            self.assertEqual(row["removed"], 0, row)
        self.assertEqual(sync["new_total"], 0)
        self.assertEqual(api.poll(0)["stats"]["total"], total_before,
                         "синк не должен менять базу без изменений на площадке")
        # Кнопки «поставить в очередь» быть не должно.
        self.assertEqual(api.sync_queue_new()["error"],
                         "Новых для загрузки нет")


class TestLiveAccountPlaylist(LiveCase):
    """Плейлист тестового аккаунта: добавление и пробное скачивание.

    Плейлист unlisted - метаданные видны без входа (проверено), но сама
    цель теста - увидеть, упирается ли СКАЧИВАНИЕ в «подтвердите, что не
    бот» без привязанного аккаунта. Если упадёт - причина будет в статусе.
    """

    PLAYLIST_URL = ("https://youtube.com/playlist?list="
                    "PLK9-A291zlJAa0wsWL3ew_AJnJQc6sgYz")

    def test_add_and_download_shortest(self):
        api = self.make_api()
        (self.dir / "lib").mkdir()
        storage = self.add_storage(api, self.dir / "lib")
        api.save_setting({"key": "default_storage_id",
                          "value": storage["id"]})

        # Фазы A/B/C: добавляем источник без аккаунта (он unlisted).
        self.assertTrue(api.add_start({"url": self.PLAYLIST_URL,
                                       "mode": "manual"})["ok"])
        flow = self.wait(api, "confirm")
        counts = flow["plan"]["counts"]
        self.assertGreaterEqual(counts["new_videos"], 5,
                                "плейлист должен отдаваться целиком")
        self.assertTrue(api.add_confirm()["ok"])
        self.wait(api, "done")
        self.assertEqual(api.poll(0)["stats"]["playlists"], 1)

        # Пробное скачание самой короткой записи - реальной качалкой.
        row = api.db.conn.execute(
            "SELECT id, duration_s FROM videos WHERE duration_s > 0 "
            "ORDER BY duration_s LIMIT 1").fetchone()
        self.assertIsNotNone(row, "нет записей с длительностью")
        result = api.enqueue({"ids": [row["id"]]})
        self.assertEqual(result.get("queued"), 1, result)
        started = api.queue_start()
        self.assertTrue(started.get("ok"), started)
        self.assertTrue(api.dl.wait(timeout=420), "воркер не завершился")

        state = api.db.conn.execute(
            "SELECT status, last_error FROM videos WHERE id=?",
            (row["id"],)).fetchone()
        files = [dict(item) for item in api.db.conn.execute(
            "SELECT path, size FROM files WHERE video_id=? AND kind='video'",
            (row["id"],))]
        self.assertEqual(
            state["status"], "downloaded",
            f"статус {state['status']}, причина: {state['last_error']}")
        self.assertTrue(files and files[0]["size"] > 0,
                        f"файл должен лежать на диске: {files}")
        self.assertGreater(files[0]["size"], 100_000,
                           "похоже, скачался не файл, а заглушка")


if __name__ == "__main__":
    unittest.main()
