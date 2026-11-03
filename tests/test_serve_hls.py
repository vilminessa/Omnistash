"""SV-C S3: HLS-конвейер для клиента (docs/SERVER-CLIENT.md §6).

Проба решает кодек, ffmpeg пишет плейлист и сегменты, кеш переиспользуется по
сигнатуре файла, отказ называет кодек или инструмент. Реальные ffmpeg/ffprobe
подменяются (`hls._run`): проверка не зависит от того, установлен ли ffmpeg на
машине, и не жжёт CPU на тесты.

Что проверяется:
  * copy-перекладка h264+aac: плейлист `index.m3u8` (media-playlist, который
    пишет ffmpeg), сегменты `seg<N>.ts`, `-c copy`, кеш reuse по сигнатуре;
  * rebuild по новой сигнатуре файла; инвалидация хуками окна;
  * отказ HEVC/VP9/AV1 называет кодек; ре-encode только по настройке и при
    наличии энкодера, без энкодера - отказ называет энкодер;
  * «ffmpeg не установлен» / «кодек не прочитан» - честный отказ, не молчание;
  * маршрут читает только кеш этого видео: чужое имя и «../» - 404 по подписи;
  * право `watch`, зрители: сессии плеера и слоты раздачи под одним лимитом,
    TTL и снятие слотов удалённой строки;
  * замок на видео: второй зритель ждёт перекладку и reuse кеш, не дублирует;
  * журнал: маршрут и право, без путей дисков и значений токенов;
  * проводка: настройки читаются при запросе (transcode без перезапуска),
    хуки repack/migrate/rebind/удаления вызывают сброс кеша.
"""

import http.client
import json
import re
import ssl
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from app import audio as audio_mod
from app import downloader as downloader_mod
from app import i18n, repo
from app.http import audit as serve_audit
from app.http import hls as serve_hls
from app.http import server as serve_http
from app.http import tokens as serve_tokens
from app.http import tls as serve_tls
from tests.test_gui import GuiCase, wait_until

SRC = Path(__file__).resolve().parent.parent
GUI = SRC / "app" / "gui.py"

PLAYLIST = ("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-PLAYLIST-TYPE:VOD\n"
            "#EXT-X-TARGETDURATION:4\n#EXTINF:4.0,\nseg0.ts\n"
            "#EXTINF:4.0,\nseg1.ts\n#EXT-X-ENDLIST\n")
SEGMENT_DATA = b"\x47" * 188      # MPEG-TS: пакеты начинаются с 0x47


class FakeProc:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def gui_body(text: str, name: str) -> str:
    """Тело метода `name` из app/gui.py (до следующего `def`)."""
    start = text.index(f"def {name}(")
    nxt = text.find("\n    def ", start + 1)
    return text[start:nxt if nxt != -1 else len(text)]


class HlsCase(GuiCase):
    """Живой слушатель поверх Api с подменой ffmpeg/ffprobe и кешем во временной папке."""

    def setUp(self):
        super().setUp()
        cache = self.dir / "hls_cache"
        patcher = mock.patch.object(serve_hls, "hls_cache_dir", lambda: cache)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(serve_tokens, "serve_tokens_path",
                                    lambda: self.dir / "serve_tokens.bin")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(serve_audit, "serve_audit_path",
                                    lambda: self.dir / "serve_audit.log")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(serve_tls, "profile_dir", lambda: self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

        # Инструменты и запуск процесса - подменные: тест не трогает настоящий
        # ffmpeg и не зависит от его установки на машине.
        self.ffprobe = "ffprobe.exe"
        self.ffmpeg = "ffmpeg.exe"
        self.encoders: list[str] = ["libx264", "aac"]
        self.kinds: dict | None = {"video": "h264", "audio": "aac"}
        self.build_fail = False
        self.build_sleep = 0.0
        self.build_started = threading.Event()
        self.probes: list[list[str]] = []
        self.builds: list[tuple[list[str], str]] = []
        patcher = mock.patch.object(audio_mod, "find_ffprobe",
                                    lambda: self.ffprobe)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(downloader_mod, "find_ffmpeg",
                                    lambda: self.ffmpeg)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(downloader_mod, "available_transcoders",
                                    lambda ffmpeg=None: list(self.encoders))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(serve_hls, "_run", self.fake_run)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.cache = cache
        self.api = self.make_api()
        self.conn = self.api.db.conn
        self.store = serve_tokens.TokenStore(self.dir / "serve_tokens.bin")
        self.viewer = self.store.issue(["watch"], label="зритель")
        self.card = self.store.issue(["read"], label="карточка")
        self.journal = serve_audit.Audit(self.dir / "serve_audit.log")
        self.notes: list[str] = []
        self.server = serve_http.ServeServer(
            self.api, bind="127.0.0.1", port=0, rate_bps=0, watchers_max=2,
            tokens=self.store, audit=self.journal,
            settings=self.api._current_settings, language=lambda: "ru",
            log=self.notes.append)
        self.port = self.server.start()
        self.addCleanup(self.server.stop)
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE

    # -- подменный процесс ---------------------------------------------------

    def fake_run(self, cmd, timeout, cwd=None):
        if cmd[0].startswith("ffprobe"):
            self.probes.append(list(cmd))
            if self.kinds is None:
                return FakeProc(1, stderr=b"ffprobe: bad file")
            streams = []
            if self.kinds.get("video"):
                streams.append({"codec_type": "video",
                                "codec_name": self.kinds["video"]})
            if self.kinds.get("audio"):
                streams.append({"codec_type": "audio",
                                "codec_name": self.kinds["audio"]})
            return FakeProc(0, stdout=json.dumps({"streams": streams}).encode())
        # ffmpeg-перекладка
        self.build_started.set()
        if self.build_sleep:
            time.sleep(self.build_sleep)
        if self.build_fail:
            self.builds.append((list(cmd), str(cwd)))
            return FakeProc(1, stderr=b"ffmpeg: unexpected end of file")
        directory = Path(cwd)
        self.builds.append((list(cmd), str(directory)))
        (directory / serve_hls.PLAYLIST_NAME).write_text(PLAYLIST, encoding="utf-8")
        (directory / "seg0.ts").write_bytes(SEGMENT_DATA)
        (directory / "seg1.ts").write_bytes(SEGMENT_DATA)
        return FakeProc(0)

    # -- помощь --------------------------------------------------------------

    def video(self, key: str, title: str) -> int:
        vid, _ = repo.upsert_video(self.conn, {
            "platform": "youtube", "remote_id": key, "key": f"youtube:{key}",
            "title": title, "raw_json": "{}", "origin": "yt-dlp"})
        return vid

    def track(self, video_id: int, name: str | None = None,
              data: bytes = b"\x00" * 4096) -> int:
        # Имя файла уникально для строки: files.path UNIQUE, и одинаковое имя
        # перепривязало бы файл на другую строку (row «не скачано» ложно).
        path = self.dir / (name or f"track-{video_id}.mp4")
        path.write_bytes(data)
        repo.record_file(self.conn, video_id, str(path), "video",
                         size=len(data), mtime=1.0)
        return int(self.conn.execute("SELECT id FROM files WHERE path=?",
                                     (str(path),)).fetchone()["id"])

    def get(self, path: str, token: str | None = None, method: str = "GET"):
        conn = http.client.HTTPSConnection("127.0.0.1", self.port, timeout=15,
                                          context=self.ctx)
        try:
            headers = {} if token is None else {"Authorization": "Bearer " + token}
            conn.request(method, path, headers=headers)
            response = conn.getresponse()
            return response.status, response.headers, response.read()
        finally:
            conn.close()

    def playlist(self, video_id: int, token=None):
        return self.get(f"/hls/{video_id}/{serve_hls.PLAYLIST_NAME}",
                        token=self.viewer.value if token is None else token)

    def problem(self, body: bytes) -> dict:
        return json.loads(body.decode("utf-8"))

    # -- перекладка copy ------------------------------------------------------

    def test_copy_remux_serves_playlist_and_segments(self):
        vid = self.video("hls-copy", "h264+aac")
        self.track(vid)
        status, headers, body = self.playlist(vid)
        self.assertEqual(status, 200, body[:60].decode("utf-8", errors="replace"))
        self.assertEqual(headers.get("Content-Type"), "application/vnd.apple.mpegurl")
        self.assertIn("#EXTM3U", body.decode("utf-8"))
        self.assertEqual(headers.get("Cache-Control"), "no-store",
                         "кеш HLS кешится: rebuild по новой сигнатуре не виден")

        status, headers, seg = self.get(f"/hls/{vid}/seg0.ts", token=self.viewer.value)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "video/mp2t")
        self.assertEqual(seg, SEGMENT_DATA)

        # Проба прочитана, команда copy: -c copy, vod-плейлист, сегменты seg%d.ts.
        self.assertEqual(len(self.probes), 1)
        cmd, cwd = self.builds[0]
        self.assertIn("-c", cmd)
        self.assertEqual(cmd[cmd.index("-c") + 1], "copy",
                         "h264+aac перекладываются копированием, не ре-encode")
        self.assertEqual(cmd[cmd.index("-hls_playlist_type") + 1], "vod")
        self.assertEqual(cmd[cmd.index("-hls_segment_filename") + 1], "seg%d.ts")
        self.assertEqual(cmd[-1], serve_hls.PLAYLIST_NAME)
        self.assertEqual(Path(cwd), self.cache / str(vid),
                         "кеш пишет не в папку этого видео")
        meta = serve_hls.read_meta(vid)
        self.assertEqual(meta["mode"], "copy")

    def test_cache_reused_until_signature_changes(self):
        vid = self.video("hls-reuse", "h264+aac")
        file_id = self.track(vid)
        self.playlist(vid)
        self.playlist(vid)
        self.get(f"/hls/{vid}/seg1.ts", token=self.viewer.value)
        self.assertEqual(len(self.builds), 1, "кеш не переиспользуется - ffmpeg жжёт CPU повторно")
        self.assertEqual(len(self.probes), 1)

        # Файл изменился (размер/хеш в индексе) - кеш перекладывается заново.
        self.conn.execute("UPDATE files SET size=?, hash=? WHERE id=?",
                          (9999, "sha256:" + "ee" * 32, file_id))
        status, _, _ = self.playlist(vid)
        self.assertEqual(status, 200)
        self.assertEqual(len(self.builds), 2, "новая сигнатура не перекладена")
        meta = serve_hls.read_meta(vid)
        row = self.conn.execute("SELECT size, hash, mtime FROM files WHERE id=?",
                                (file_id,)).fetchone()
        self.assertEqual(meta["signature"], serve_hls.signature(dict(row)),
                         "meta хранит старую сигнатуру - reuse был бы ложью")

    def test_build_failure_is_named_and_cache_not_claimed(self):
        """ffmpeg отказался: 400 с хвостом stderr, meta не написана (не «готово»)."""
        vid = self.video("hls-fail", "битый для ffmpeg")
        self.track(vid)
        self.build_fail = True
        status, _, body = self.playlist(vid)
        self.assertEqual(status, 400)
        payload = self.problem(body)
        self.assertEqual(payload.get("code"), "hls")
        self.assertIn(i18n.t("http.hls_failed", "ru"), payload.get("error", ""))
        self.assertIn("unexpected end of file", payload.get("error", ""),
                      "причина ffmpeg не названа клиенту")
        self.assertIsNone(serve_hls.read_meta(vid),
                          "meta написана при неудачной перекладке")

    # -- отказ по кодексу -----------------------------------------------------

    def test_refusal_names_codec_without_transcode(self):
        for codec in ("hevc", "vp9", "av1"):
            with self.subTest(codec=codec):
                vid = self.video(f"hls-{codec}", "дорожка")
                self.track(vid)
                self.kinds = {"video": codec, "audio": "aac"}
                status, _, body = self.playlist(vid)
                self.assertEqual(status, 400)
                payload = self.problem(body)
                self.assertEqual(payload.get("code"), "hls")
                self.assertIn(i18n.t("http.hls_codec", "ru"), payload.get("error", ""),
                              "отказ не говорит, что браузер не играет кодек")
                self.assertIn(codec, payload.get("error", ""),
                              "отказ не называет сам кодек")
                self.assertEqual(self.builds, [],
                                 "ре-encode начат без явной настройки")
                self.assertIsNone(serve_hls.read_meta(vid))
                self.builds.clear()

    def test_transcode_only_with_flag_and_encoder(self):
        vid = self.video("hls-transcode", "hevc")
        self.track(vid)
        self.kinds = {"video": "hevc", "audio": "aac"}
        self.api.save_setting({"key": "serve_hls_transcode", "value": True})
        status, _headers, body = self.playlist(vid)
        self.assertEqual(status, 200, body[:60].decode("utf-8", errors="replace"))
        cmd, _cwd = self.builds[0]
        self.assertEqual(cmd[cmd.index("-c:v") + 1], serve_hls.TRANSCODE_VIDEO)
        self.assertNotIn("-c", cmd, "ре-encode одновременно с -c copy")
        self.assertEqual(serve_hls.read_meta(vid)["mode"], "transcode")

    def test_transcode_without_encoder_names_the_encoder(self):
        vid = self.video("hls-noenc", "hevc")
        self.track(vid)
        self.kinds = {"video": "hevc", "audio": "aac"}
        self.api.save_setting({"key": "serve_hls_transcode", "value": True})
        self.encoders = []          # сборка ffmpeg без libx264
        status, _, body = self.playlist(vid)
        self.assertEqual(status, 400)
        payload = self.problem(body)
        self.assertIn(i18n.t("http.hls_encoder", "ru"), payload.get("error", ""))
        self.assertIn(serve_hls.TRANSCODE_VIDEO, payload.get("error", ""),
                      "отказ не называет, какой энкодер нужен")
        self.assertEqual(self.builds, [], "ffmpeg запущен без энкодера")

    def test_missing_tools_are_named_not_silent(self):
        vid = self.video("hls-notools", "дорожка")
        self.track(vid)
        self.ffmpeg = None
        status, _, body = self.playlist(vid)
        self.assertEqual(status, 400)
        self.assertIn(i18n.t("http.hls_no_ffmpeg", "ru"), self.problem(body).get("error", ""),
                      "без ffmpeg сервер обязан сказать «не установлен», а не молчать")
        self.assertEqual(self.probes, [], "без ffmpeg проба всё равно запущена")

        self.ffmpeg = "ffmpeg.exe"
        self.ffprobe = None
        status, _, body = self.playlist(vid)
        self.assertEqual(status, 400)
        self.assertIn(i18n.t("http.hls_probe_failed", "ru"),
                      self.problem(body).get("error", ""),
                      "без ffprobe кодек не «угадан»: отказ называет пробу")

    # -- маршрут читает только свой кеш ---------------------------------------

    def test_foreign_names_and_walk_are_refused_by_name(self):
        vid = self.video("hls-name", "дорожка")
        self.track(vid)
        self.playlist(vid)                       # кеш построен
        for name in ("master.m3u8", "meta.json", "seg0.mp4", "secret.txt",
                     "../../secret.txt"):
            status, _, body = self.get(f"/hls/{vid}/{name}", token=self.viewer.value)
            self.assertEqual(status, 404, f"{name}: чужое имя прочитано")
            self.assertIn(i18n.t("http.hls_segment", "ru"),
                          self.problem(body).get("error", ""),
                          f"{name}: отказ не называет, что не найдено в кеше")
        status, _, body = self.get("/hls/abc/index.m3u8", token=self.viewer.value)
        self.assertEqual(status, 404)
        self.assertIn("нет видео", self.problem(body).get("error", ""))

    def test_no_video_on_disk_is_named_not_empty_playlist(self):
        vid = self.video("hls-nofile", "не скачано")
        status, _, body = self.playlist(vid)
        self.assertEqual(status, 404)
        payload = self.problem(body)
        self.assertEqual(payload.get("code"), "missing")
        self.assertIn("не скачано", payload.get("error", ""),
                      "пустой плейлист вместо «видео не скачано» - плеер молчит")

    def test_gone_from_disk_reported_as_gone(self):
        vid = self.video("hls-gone", "дорожка пропала")
        self.track(vid)
        self.playlist(vid)
        (self.dir / f"track-{vid}.mp4").unlink()
        # Сигнатура в meta та же, а файла нет: кеш не отдаётся «по памяти».
        status, _, body = self.get(f"/hls/{vid}/seg0.ts", token=self.viewer.value)
        self.assertEqual(status, 404)
        self.assertIn("пропал с диска", self.problem(body).get("error", ""))

    # -- права и зрители -------------------------------------------------------

    def test_watch_scope_and_pairing_gate(self):
        vid = self.video("hls-scope", "дорожка")
        self.track(vid)
        status, _, body = self.playlist(vid, token=self.card.value)
        self.assertEqual(status, 403)
        payload = self.problem(body)
        self.assertEqual(payload.get("scope"), "watch",
                         "байты HLS - право watch (§4)")
        status, _, body = self.playlist(vid, token="")
        self.assertEqual(status, 401)
        self.assertEqual(self.problem(body).get("code"), "pairing")

    def test_sessions_and_slots_share_one_limit(self):
        vid_a = self.video("hls-lim-a", "дорожка A")
        self.track(vid_a)
        vid_b = self.video("hls-lim-b", "дорожка B")
        self.track(vid_b)
        self.assertEqual(self.server._viewers.limit, 2)
        self.assertEqual(self.playlist(vid_a)[0], 200)
        self.assertEqual(self.playlist(vid_b)[0], 200)
        # Тот же плеер того же видео - продление, а не новый зритель.
        self.assertEqual(self.playlist(vid_a)[0], 200)
        self.assertEqual(self.server._viewers.count(), 2)
        vid_c = self.video("hls-lim-c", "дорожка C")
        self.track(vid_c)
        status, _, body = self.playlist(vid_c)
        self.assertEqual(status, 429, "третий плеер принят сверх лимита")
        payload = self.problem(body)
        self.assertEqual(payload.get("code"), "rate")
        self.assertIn("максимум 2", payload.get("error", ""))

        # TTL истёк - слот свободен без ожидания: помечаем сессии старыми.
        for pair, stamp in list(self.server._viewers._sessions.items()):
            self.server._viewers._sessions[pair] = stamp - 400
        status, _, _ = self.playlist(vid_c)
        self.assertEqual(status, 200, "обрезание по TTL не освободил слот")

    def test_drop_video_frees_the_slot(self):
        vid = self.video("hls-drop", "дорожка")
        self.track(vid)
        self.playlist(vid)
        self.assertEqual(self.server._viewers.count(), 1)
        self.server.drop_video(vid)
        self.assertEqual(self.server._viewers.count(), 0,
                         "строка убрана - плеер держал бы слот до TTL")

    def test_slot_of_held_media_blocks_hls_and_vice_versa(self):
        """Раздача /media держит слот до конца - HLS сверх лимита получает 429."""
        import socket
        vid_media = self.video("hls-slot-m", "большая дорожка")
        file_id = self.track(vid_media, data=b"\x00" * (3 * 1024 * 1024))
        vid_hls = self.video("hls-slot-h", "дорожка")
        self.track(vid_hls, name="other.mp4")
        held = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        tls = self.ctx.wrap_socket(held, server_hostname="localhost")
        tls.sendall(f"GET /media/{file_id} HTTP/1.1\r\nHost: localhost\r\n"
                    f"Authorization: Bearer {self.viewer.value}\r\n\r\n".encode())
        self.assertTrue(wait_until(lambda: self.server._viewers.count() == 1,
                                   timeout=6.0, message="раздача не держала слот"),
                        "раздача /media не заняла слот - проверка была бы на пустом месте")
        # Одна сессия HLS ещё влезает (лимит 2), вторая - нет.
        self.assertEqual(self.playlist(vid_hls)[0], 200)
        vid_hls2 = self.video("hls-slot-h2", "дорожка 2")
        self.track(vid_hls2, name="third.mp4")
        status, _, body = self.playlist(vid_hls2)
        self.assertEqual(status, 429, "сессия HLS принята, когда слоты исчерпаны")
        tls.close()

    def test_second_viewer_waits_for_build_and_reuses_cache(self):
        """Замок на видео: параллельные плееры перекладывают файл один раз."""
        vid = self.video("hls-lock", "дорожка")
        self.track(vid)
        self.build_sleep = 0.7
        results: list[tuple[int, str]] = []

        def watch():
            status, _headers, body = self.playlist(vid)
            results.append((status, body.decode("utf-8", errors="replace")[:120]))

        first = threading.Thread(target=watch, daemon=True)
        first.start()
        self.assertTrue(self.build_started.wait(timeout=5.0),
                        "перекладка не началась - замок не проверялся бы")
        second = self.playlist(vid)          # ждёт первый, потом берёт кеш
        first.join(timeout=10)
        results.append((second[0], second[2][:120]))
        statuses = [code for code, _text in results]
        self.assertEqual(statuses, [200, 200],
                         f"плееры не получили кеш: {results}")
        self.assertEqual(len(self.builds), 1,
                         "второй зритель перекладывает тот же файл параллельно")

    def test_head_answers_headers_without_body(self):
        vid = self.video("hls-head", "дорожка")
        self.track(vid)
        status, headers, body = self.get(f"/hls/{vid}/{serve_hls.PLAYLIST_NAME}",
                                         token=self.viewer.value, method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "application/vnd.apple.mpegurl")
        self.assertTrue(headers.get("Content-Length"))
        self.assertEqual(body, b"", "HEAD с телом - это не HEAD")

    # -- журнал ---------------------------------------------------------------

    def test_audit_names_hls_route_without_paths(self):
        vid = self.video("hls-audit", "дорожка")
        self.track(vid)
        self.playlist(vid)
        self.get(f"/hls/{vid}/seg0.ts", token=self.viewer.value)
        blob = "\n".join(self.journal.tail(30))
        self.assertIn("ключ зритель", blob)
        self.assertIn("право watch", blob)
        self.assertIn("hls", blob)
        self.assertNotIn(self.viewer.value, blob)
        self.assertNotIn(str(self.cache), blob,
                         "путь кеша дисков в журнале - путь не должен уходить")
        self.assertFalse(re.search(r"[A-Za-z]:\\", blob),
                         f"путь в журнале: {blob[:200]}")

    # -- хуки инвалидации в окне ----------------------------------------------

    def test_invalidation_rebuilds_after_hook(self):
        vid = self.video("hls-invalid", "дорожка")
        self.track(vid)
        self.playlist(vid)
        self.assertTrue((self.cache / str(vid) / serve_hls.PLAYLIST_NAME).exists())
        self.api._invalidate_hls([vid], "перенос (тест)")
        self.assertFalse((self.cache / str(vid)).exists(),
                         "кеш не сброшен - клиент читал бы старые сегменты")
        self.assertEqual(len(self.builds), 1)
        status, _, _ = self.playlist(vid)
        self.assertEqual(status, 200)
        self.assertEqual(len(self.builds), 2, "после сброса кеш не перекладен заново")
        # Кеш, которого не было: invalidate не выдумывает «сброшен».
        self.assertFalse(serve_hls.invalidate(999999, "нет кеша"))

    def test_hooks_are_wired_in_the_backend(self):
        """Удаление, перенос, переупаковка, перепривязка и дедуп сбрасывают кеш."""
        text = GUI.read_text(encoding="utf-8")
        for name in ("bulk_remove_rows", "dedupe_resolve", "rebind_file",
                     "_migrate_worker", "_repack_worker"):
            body = gui_body(text, name)
            self.assertIn("_invalidate_hls(", body,
                          f"{name}: HLS-кеш больше не сбрасывается при изменении файлов")
        self.assertIn('plan.get("files")', gui_body(text, "_migrate_worker"),
                      "перенос не знает, какие видео переехали")
        self.assertIn('plan.get("items")', gui_body(text, "_repack_worker"),
                      "переупаковка не знает, какие видео переименованы")
        self.assertIn("old_owner", gui_body(text, "rebind_file"),
                      "перепривязка не сбрасывает кеш старого видео")

    def test_settings_read_per_request_without_restart(self):
        """serve_hls_transcode включается в карточке - слушатель не перезапускается."""
        vid = self.video("hls-live", "hevc")
        self.track(vid)
        self.kinds = {"video": "hevc", "audio": "aac"}
        status, _, body = self.playlist(vid)
        self.assertEqual(status, 400)
        self.assertIn(i18n.t("http.hls_codec", "ru"), self.problem(body).get("error", ""))
        self.api.save_setting({"key": "serve_hls_transcode", "value": True})
        status, _, _ = self.playlist(vid)
        self.assertEqual(status, 200,
                         "настройка не прочитана при запросе: нужен перезапуск слушателя")


class TestHlsHelpers(unittest.TestCase):
    """Решение пробы, сигнатура и имена кеша - проверяются без слушателя."""

    def test_decide_table(self):
        copy = serve_hls.decide({"video": "h264", "audio": "aac"}, False, [])
        self.assertEqual(copy, ("copy", ""),
                         "h264+aac перекладываются копированием")
        self.assertEqual(serve_hls.decide({"video": "h264", "audio": None}, False, []),
                         ("copy", ""), "дорожка без аудио - copy допустим")
        refuse = serve_hls.decide({"video": "hevc", "audio": "aac"}, False, ["libx264"])
        self.assertEqual(refuse[0], "")
        self.assertIn("hevc", refuse[1])
        transcode = serve_hls.decide({"video": "vp9", "audio": "opus"}, True, ["libx264"])
        self.assertEqual(transcode[0], "transcode",
                         "настройка включена и энкодер есть - ре-encode право")
        no_encoder = serve_hls.decide({"video": "vp9", "audio": "opus"}, True, [])
        self.assertEqual(no_encoder[0], "")
        self.assertIn(serve_hls.TRANSCODE_VIDEO, no_encoder[1])

    def test_signature_keys_the_file_content(self):
        row = {"size": 100, "mtime": 7.5, "hash": "sha256:" + "aa" * 32}
        sig = serve_hls.signature(row)
        self.assertEqual(sig, serve_hls.signature(dict(row)))
        self.assertNotEqual(sig, serve_hls.signature({**row, "size": 101}))
        self.assertNotEqual(sig, serve_hls.signature({**row, "hash": ""}),
                            "сигнатура без известного хеша совпала бы с хешем")

    def test_playlist_and_segment_names_are_what_ffmpeg_writes(self):
        """Мы не сочиняем формат: ffmpeg пишет media-playlist и seg<N>.ts."""
        self.assertEqual(serve_hls.PLAYLIST_NAME, "index.m3u8")
        self.assertTrue(serve_hls.SEGMENT.fullmatch("seg0.ts"))
        self.assertTrue(serve_hls.SEGMENT.fullmatch("seg123.ts"))
        self.assertFalse(serve_hls.SEGMENT.fullmatch("seg0.mp4"))
        self.assertFalse(serve_hls.SEGMENT.fullmatch("master.m3u8"))
        cmd = serve_hls._ffmpeg_cmd("copy", "in.mkv")
        self.assertIn("-hls_playlist_type", cmd)
        self.assertEqual(cmd[cmd.index("-hls_playlist_type") + 1], "vod",
                         "live-плейлист обещает сегменты, которых нет у VOD")
        self.assertEqual(cmd[-1], serve_hls.PLAYLIST_NAME)
        transcode_cmd = serve_hls._ffmpeg_cmd("transcode", "in.mkv")
        self.assertIn("-c:v", transcode_cmd)
        self.assertEqual(transcode_cmd[transcode_cmd.index("-c:v") + 1],
                         serve_hls.TRANSCODE_VIDEO)


if __name__ == "__main__":
    unittest.main()
