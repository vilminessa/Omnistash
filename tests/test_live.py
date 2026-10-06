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


if __name__ == "__main__":
    unittest.main()
