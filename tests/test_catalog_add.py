"""Мгновенное добавление видео с карточки каталога (CAT-3).

Карточка одиночного видео пишет его сразу: экран «Добавить источник» с
планом из одной записи ничего не решает - пользователь уже сказал «да».
Плейлист и канал под auto остаются с подтверждением: там план на десятки
записей надо показывать, а не принимать молча.
"""

import time
import unittest
from pathlib import Path
from unittest import mock

from app import downloader
from app.gui import Api
from app.sources import FetchError, failure_message
from tests.test_add_flow import fixture_snapshot
from tests.test_gui import GuiCase

WATCH_URL = "https://www.youtube.com/watch?v=vidfixtur01"
PLAYLIST_URL = "https://youtube.com/playlist?list=PLfixtur0000000000000000000001"
PH_URL = "https://www.pornhub.com/view_video.php?viewkey=ph698a3d3b0e335"


def video_snapshot(url=WATCH_URL, remote_id="vidfixtur01", platform="youtube"):
    """Снапшот формы «одно видео» - то, что отдаёт sources._snapshot_video."""
    snap = fixture_snapshot(playlist_id=remote_id, count=1)
    snap["playlist"].update(kind="video", platform=platform,
                            remote_id=remote_id, title="Клип из каталога",
                            url=url)
    snap["entries"][0]["remote_id"] = remote_id
    snap["entries"][0]["webpage_url"] = url
    snap["url"] = url
    return snap


class CatalogAddCase(GuiCase):
    def setUp(self):
        super().setUp()
        # Факты согласий лежат в профиле: без патча тест читал бы реальное
        # «мне 18+» пользователя и гейт не срабатывал бы.
        patcher = mock.patch("app.legal.legal_path",
                             return_value=self.dir / "legal.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def api(self) -> Api:
        return self.make_api()

    def default_storage(self, api) -> dict:
        """Папка-хранилище по умолчанию: очередь качает в неё, а не в никуда."""
        (self.dir / "out").mkdir(exist_ok=True)
        storage = self.add_storage(api, self.dir / "out", label="Тестовая папка")
        api.save_setting({"key": "default_storage_id", "value": storage["id"]})
        return storage

    def patch_fetch(self, snapshot, delay=0.0):
        """Подмена снапшота; возвращает список ссылок, до которых дошла сеть."""
        calls: list[str] = []

        def fake(url, settings=None, on_progress=None, stop=None,
                 account_id=None):
            calls.append(url)
            if on_progress:
                on_progress(0, snapshot["total"])
            if delay:
                time.sleep(delay)
            return snapshot

        patcher = mock.patch("app.gui.sources.fetch_snapshot", side_effect=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def fake_download(self):
        """Очередь в офлайн-прогоне не должна трогать настоящую площадку."""
        def download(video, settings, *, stop, on_progress=None, dest=None,
                     **kwargs):
            target = Path(dest or self.dir / "out")
            target.mkdir(parents=True, exist_ok=True)
            path = target / "клип.mp4"
            path.write_bytes(b"payload")
            return {"cancelled": False, "files": [(str(path), "video")],
                    "info": {}, "error": None, "hash": "sha256:aa"}
        patcher = mock.patch.object(downloader, "download", side_effect=download)
        patcher.start()
        self.addCleanup(patcher.stop)

    def phases_until(self, api, final, timeout=15.0):
        """Фазы, которые интерфейс успел увидеть, до финальной включительно."""
        seen: list[str] = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            flow = api.poll(0)["add_flow"]
            if flow["phase"] not in seen:
                seen.append(flow["phase"])
            if flow["phase"] == final:
                return seen, flow
            if final != "error" and flow["phase"] == "error":
                self.fail(f"ошибка вместо «{final}»: {flow.get('error')}")
            time.sleep(0.02)
        self.fail(f"фаза {final} не наступила, сейчас "
                  f"{api.poll(0)['add_flow']['phase']}")


class TestDirectAdd(CatalogAddCase):
    def test_video_card_never_shows_plan_screen(self):
        api = self.api()
        storage = self.default_storage(api)
        self.fake_download()
        calls = self.patch_fetch(video_snapshot(), delay=0.3)
        api.save_setting({"key": "default_sync_mode", "value": "partial"})

        self.assertTrue(api.add_start({"url": WATCH_URL, "auto": True,
                                       "storage_id": storage["id"]})["ok"])
        seen, flow = self.phases_until(api, "done")

        self.assertNotIn("confirm", seen,
                         "карточка каталога не должна показывать «Точно добавить?»")
        self.assertEqual(calls, [WATCH_URL], "запрос площадки ровно один")
        self.assertEqual(flow["result"]["stats"]["new_videos"], 1)
        self.assertEqual(api.poll(0)["stats"]["total"], 1)
        self.assertTrue(flow["auto"], "флоу помнит, что это карточка каталога")

    def test_partial_mode_for_one_video_queues_it(self):
        """«Частичная» для одного видео = пикер из одного элемента: вместо
        лишнего экрана видео встаёт в очередь («Полная»)."""
        api = self.api()
        storage = self.default_storage(api)
        self.fake_download()
        self.patch_fetch(video_snapshot())
        api.save_setting({"key": "default_sync_mode", "value": "partial"})
        api.add_start({"url": WATCH_URL, "auto": True,
                       "storage_id": storage["id"]})
        _seen, flow = self.phases_until(api, "done")
        self.assertEqual(flow["mode"], "full")
        self.assertEqual(flow["result"]["queued"], 1)
        self.assertEqual(flow["result"]["picker_total"], 0,
                         "пикер на одну запись не плодится")
        self.assertEqual(flow["result"]["storage_label"], "Тестовая папка")
        row = api.db.conn.execute(
            "SELECT status FROM videos WHERE key=?",
            ("youtube:vidfixtur01",)).fetchone()
        self.assertNotEqual(row["status"], "known",
                            "режим «Полная» должен ставить видео в очередь")

    def test_manual_mode_writes_metadata_only(self):
        api = self.api()
        self.patch_fetch(video_snapshot())
        api.save_setting({"key": "default_sync_mode", "value": "manual"})
        api.add_start({"url": WATCH_URL, "auto": True})
        _seen, flow = self.phases_until(api, "done")
        self.assertEqual(flow["mode"], "manual")
        self.assertEqual(flow["result"]["queued"], 0)
        row = api.db.conn.execute(
            "SELECT status FROM videos WHERE key=?",
            ("youtube:vidfixtur01",)).fetchone()
        self.assertEqual(row["status"], "known",
                         "режим «Ручная» не ставит видео в очередь")

    def test_second_card_reports_already_without_writing(self):
        """Видео уже в библиотеке: вторая попытка не пишет и не качает."""
        api = self.api()
        storage = self.default_storage(api)
        self.fake_download()
        calls = self.patch_fetch(video_snapshot())
        api.add_start({"url": WATCH_URL, "auto": True,
                       "storage_id": storage["id"]})
        self.phases_until(api, "done")
        api.add_start({"url": WATCH_URL, "auto": True,
                       "storage_id": storage["id"]})
        _seen, flow = self.phases_until(api, "done")
        self.assertTrue(flow["result"]["already"], flow["result"])
        self.assertEqual(flow["result"]["queued"], 0)
        self.assertEqual(api.poll(0)["stats"]["total"], 1,
                         "строка не плодится")
        self.assertEqual(len(calls), 2, "площадка всё же спросила дважды")

    def test_playlist_under_auto_still_confirms(self):
        """Плейлист под auto молча не принимается: план на десятки записей
        пользователь должен видеть."""
        api = self.api()
        self.patch_fetch(fixture_snapshot())
        api.add_start({"url": PLAYLIST_URL, "auto": True})
        seen, flow = self.phases_until(api, "confirm")
        self.assertIn("confirm", seen)
        self.assertEqual(flow["plan"]["counts"]["new_videos"], 3)
        self.assertEqual(api.poll(0)["stats"]["total"], 0,
                         "до подтверждения БД пуста")
        api.add_confirm()
        self.phases_until(api, "done")


class TestAdultAndBusy(CatalogAddCase):
    def test_adult_gate_before_any_request(self):
        api = self.api()
        storage = self.default_storage(api)
        calls = self.patch_fetch(video_snapshot(url=PH_URL,
                                               remote_id="ph698a3d3b0e335",
                                               platform="pornhub"))
        res = api.add_start({"url": PH_URL, "auto": True,
                             "storage_id": storage["id"]})
        self.assertTrue(res.get("adult_confirm"), res)
        self.assertEqual(calls, [], "без подтверждения 18+ площадка не трогается")

        self.fake_download()
        api.legal_accept({"adult": True, "aup": True})
        api.add_start({"url": PH_URL, "auto": True,
                       "storage_id": storage["id"]})
        _seen, flow = self.phases_until(api, "done")
        self.assertEqual(flow["result"]["stats"]["new_videos"], 1)
        self.assertEqual(calls, [PH_URL])

    def test_second_add_is_refused_while_one_runs(self):
        """Один флоу на окно: UI ставит вторую карточку в очередь, а не
        просит площадку параллельно."""
        api = self.api()
        storage = self.default_storage(api)
        self.fake_download()
        self.patch_fetch(video_snapshot(), delay=0.4)
        self.assertTrue(api.add_start({"url": WATCH_URL, "auto": True,
                                       "storage_id": storage["id"]})["ok"])
        res = api.add_start({"url": "https://www.youtube.com/watch?v=vidfixtur02",
                             "auto": True, "storage_id": storage["id"]})
        self.assertIn("уже идёт", res.get("error") or "")
        self.phases_until(api, "done")


class TestAutoErrors(CatalogAddCase):
    def test_unavailable_card_reports_kind(self):
        api = self.api()
        boom = FetchError(failure_message("unavailable", "Offline"),
                          kind="unavailable")
        with mock.patch("app.gui.sources.fetch_snapshot", side_effect=boom):
            api.add_start({"url": WATCH_URL, "auto": True})
            seen, flow = self.phases_until(api, "error")
        self.assertEqual(flow["error_kind"], "unavailable")
        self.assertIn("недоступно на площадке", flow["error"])
        self.assertNotIn("confirm", seen)
        self.assertEqual(api.poll(0)["stats"]["total"], 0,
                         "отказ не пишет в базу")

    def test_blocked_card_reports_kind(self):
        api = self.api()
        api.legal_accept({"adult": True, "aup": True})   # PH без гейта не трогается
        boom = FetchError(failure_message("blocked", "410 Gone"), kind="blocked")
        with mock.patch("app.gui.sources.fetch_snapshot", side_effect=boom):
            api.add_start({"url": PH_URL, "auto": True})
            _seen, flow = self.phases_until(api, "error")
        self.assertEqual(flow["error_kind"], "blocked")
        self.assertIn("недоступна из вашей сети", flow["error"])

    def test_bad_link_rejected_before_thread(self):
        api = self.api()
        res = api.add_start({"url": "https://example.org/page", "auto": True})
        self.assertEqual(res.get("kind"), "unsupported")


if __name__ == "__main__":
    unittest.main()
