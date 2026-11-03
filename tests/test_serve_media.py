"""SV-C S2: раздача обложек, байтов и субтитров (docs/SERVER-CLIENT.md §3, §6).

Сервер - тот же `Api` с настоящей базой и файлами на диске. Проверяем то, что
решает честность и права: маршрут отвечает по id строки (путь дисков наружу не
уходит), `Range` читается честно (206 с Content-Range, 416 на битый запрос),
обложка отдаётся с ETag и 304 вместо base64 в JSON, зрители считаются на
сервере и сверх лимита отказ называет причину, пропажа файла называется
пропажей, а не «не скачано».

Профиль (настройки, база, токены, журнал, временный PEM TLS) и файлы библиотеки
- во временную папку: тесты не пишут в настоящий %LOCALAPPDATA%\\Omnistash.
"""

import http.client
import json
import socket
import ssl
import unittest
from pathlib import Path
from unittest import mock

from app import i18n, repo
from app.http import audit as serve_audit
from app.http import media as serve_media
from app.http import server as serve_http
from app.http import tokens as serve_tokens
from app.http import tls as serve_tls
from tests.test_gui import GuiCase, wait_until

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
       b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\x0bIDATx\x9cc\x00\x01"
       b"\x00\x00\x05\x00\x00\r\r\x00\x00IEND\xaeB`\x82")


class MediaCase(GuiCase):
    """Живой слушатель поверх настоящего Api с файлами во временной папке."""

    def setUp(self):
        super().setUp()
        # Пути профиля тестов: реестр токенов, журнал аудита и временный PEM
        # TLS не должны уезжать в настоящий %LOCALAPPDATA%\Omnistash.
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

        self.api = self.make_api()
        self.conn = self.api.db.conn
        self.store = serve_tokens.TokenStore(self.dir / "serve_tokens.bin")
        self.card = self.store.issue(["read"], label="карточка")        # обложки
        self.viewer = self.store.issue(["watch"], label="зритель")      # байты
        self.journal = serve_audit.Audit(self.dir / "serve_audit.log")
        self.notes: list[str] = []
        self.server = serve_http.ServeServer(
            self.api, bind="127.0.0.1", port=0, rate_bps=0, watchers_max=1,
            tokens=self.store, audit=self.journal, language=lambda: "ru",
            log=self.notes.append)
        self.port = self.server.start()
        self.addCleanup(self.server.stop)
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE

    # -- помощь ------------------------------------------------------------

    def video(self, key: str, title: str) -> int:
        vid, _ = repo.upsert_video(self.conn, {
            "platform": "youtube", "remote_id": key, "key": f"youtube:{key}",
            "title": title, "raw_json": "{}", "origin": "yt-dlp"})
        return vid

    def file(self, video_id: int, name: str, data: bytes, kind: str) -> int:
        path = self.dir / name
        path.write_bytes(data)
        repo.record_file(self.conn, video_id, str(path), kind,
                         size=len(data), mtime=1.0)
        row = self.conn.execute("SELECT id FROM files WHERE path=?",
                                (str(path),)).fetchone()
        return int(row["id"])

    def get(self, path: str, token: str | None = None, headers: dict | None = None,
            method: str = "GET"):
        conn = http.client.HTTPSConnection("127.0.0.1", self.port, timeout=10,
                                          context=self.ctx)
        try:
            sent = dict(headers or {})
            if token is not None:
                sent["Authorization"] = "Bearer " + token
            conn.request(method, path, headers=sent)
            response = conn.getresponse()
            return response.status, response.headers, response.read()
        finally:
            conn.close()

    def json_body(self, body: bytes) -> dict:
        return json.loads(body.decode("utf-8")) if body else {}

    # -- обложки ------------------------------------------------------------

    def test_thumb_is_served_with_etag_and_no_path(self):
        vid = self.video("thumb-1", "с обложкой")
        file_id = self.file(vid, "cover.webp", PNG, "thumbnail")
        status, headers, body = self.get(f"/thumb/{vid}", token=self.card.value)
        self.assertEqual(status, 200, body[:80])
        self.assertEqual(headers.get("Content-Type"), "image/webp")
        self.assertEqual(len(body), len(PNG), "обложка урезана: картинка битая")
        self.assertTrue(headers.get("ETag"), "обложка без ETag - клиент не подтвердит 304")
        self.assertEqual(headers.get("Cache-Control"), "public, max-age=3600")
        self.assertIn("Accept-Ranges", headers)
        # Путь дисков наружу не уходит.
        blob = (str(headers) + body.decode("utf-8", errors="replace")).encode("utf-8")
        self.assertNotIn(str(self.dir).encode(), blob, "путь библиотеки в ответе")

    def test_thumb_not_modified_is_304(self):
        vid = self.video("thumb-2", "обложка")
        self.file(vid, "cover.png", PNG, "thumbnail")
        first, headers, _ = self.get(f"/thumb/{vid}", token=self.card.value)
        self.assertEqual(first, 200)
        tag = headers.get("ETag")
        status, _, body = self.get(f"/thumb/{vid}", token=self.card.value,
                                   headers={"If-None-Match": tag})
        self.assertEqual(status, 304, "повторный запрос должен быть 304, а не повторная раздача")
        self.assertEqual(body, b"", "304 с телом - плеер и картинка читают это иначе")

    def test_thumb_etag_uses_hash_when_known(self):
        vid = self.video("thumb-3", "обложка с хешем")
        path = self.dir / "hashed.webp"
        path.write_bytes(PNG)
        repo.record_file(self.conn, vid, str(path), "thumbnail",
                         size=len(PNG), mtime=1.0, digest="sha256:" + "ab" * 32)
        status, headers, _ = self.get(f"/thumb/{vid}", token=self.card.value)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("ETag"), '"abababababababababababababababab"')

    def test_thumb_missing_is_named_not_blank(self):
        vid = self.video("thumb-4", "без обложки")
        status, _, body = self.get(f"/thumb/{vid}", token=self.card.value)
        self.assertEqual(status, 404)
        self.assertEqual(self.json_body(body).get("error"),
                         i18n.t("http.thumb_missing", "ru"))
        self.assertIn("обложка не найдена", self.json_body(body).get("error"))

    def test_thumb_gone_from_disk_is_reported_as_gone(self):
        vid = self.video("thumb-5", "обложка пропала")
        self.file(vid, "gone.webp", PNG, "thumbnail")
        (self.dir / "gone.webp").unlink()
        status, _, body = self.get(f"/thumb/{vid}", token=self.card.value)
        self.assertEqual(status, 404)
        payload = self.json_body(body)
        self.assertIn("пропала с диска", payload.get("error", ""),
                      "пропажа файла - это «пропал», а не «не скачан»")

    def test_thumb_needs_read_scope(self):
        vid = self.video("thumb-6", "обложка")
        self.file(vid, "c.webp", PNG, "thumbnail")
        status, _, body = self.get(f"/thumb/{vid}", token=self.viewer.value)
        self.assertEqual(status, 403)
        payload = self.json_body(body)
        self.assertEqual(payload.get("scope"), "read",
                         "обложка - право read (часть карточки), не watch")

    def test_thumb_without_token_is_pairing(self):
        vid = self.video("thumb-7", "обложка")
        self.file(vid, "c.webp", PNG, "thumbnail")
        status, _, body = self.get(f"/thumb/{vid}", token="")
        self.assertEqual(status, 401)
        self.assertEqual(self.json_body(body).get("code"), "pairing")

    def test_thumb_id_must_be_a_number(self):
        status, _, body = self.get("/thumb/abc", token=self.card.value)
        self.assertEqual(status, 404)
        self.assertIn("нет видео", self.json_body(body).get("error", ""))

    def test_thumb_cannot_walk_the_disk(self):
        """Маршрут строится по id: чужой путь через /thumb/ не читается."""
        secret = self.dir / "secret.txt"
        secret.write_text("секрет", encoding="utf-8")
        status, _, body = self.get("/thumb/../../secret.txt", token=self.card.value)
        self.assertEqual(status, 404, self.json_body(body))
        self.assertNotIn("секрет", body.decode("utf-8", errors="replace"))

    # -- байты медиа --------------------------------------------------------

    def test_media_full_and_headers(self):
        vid = self.video("media-1", "дорожка")
        data = bytes(range(256)) * 64        # 16 КиБ
        file_id = self.file(vid, "clip.mp4", data, "video")
        status, headers, body = self.get(f"/media/{file_id}", token=self.viewer.value)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "video/mp4")
        self.assertEqual(headers.get("Accept-Ranges"), "bytes")
        self.assertEqual(len(body), len(data), "раздача урезана: плеер не досмотрит хвост")
        self.assertTrue(headers.get("ETag"))

    def test_media_audio_serves_bytes_directly(self):
        vid = self.video("media-2", "аудио")
        data = b"ID3\x03" + b"\x00" * 512
        file_id = self.file(vid, "song.mp3", data, "video")   # kind=video (E0)
        status, headers, body = self.get(f"/media/{file_id}", token=self.viewer.value)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "audio/mpeg")
        self.assertEqual(len(body), len(data))

    def test_media_range_partial(self):
        vid = self.video("media-3", "дорожка")
        data = bytes(range(256)) * 64
        file_id = self.file(vid, "clip.mp4", data, "video")
        status, headers, body = self.get(f"/media/{file_id}", token=self.viewer.value,
                                         headers={"Range": "bytes=1000-2000"})
        self.assertEqual(status, 206, "Range не читается как 206 - плеер не resume'ит")
        self.assertEqual(headers.get("Content-Range"), f"bytes 1000-2000/{len(data)}")
        self.assertEqual(body, data[1000:2001])

    def test_media_range_suffix_and_open_end(self):
        vid = self.video("media-4", "дорожка")
        data = bytes(range(256)) * 64
        file_id = self.file(vid, "clip.mp4", data, "video")
        status, headers, body = self.get(f"/media/{file_id}", token=self.viewer.value,
                                         headers={"Range": "bytes=-512"})
        self.assertEqual(status, 206)
        self.assertEqual(body, data[-512:])
        self.assertEqual(headers.get("Content-Range"),
                         f"bytes {len(data) - 512}-{len(data) - 1}/{len(data)}")
        status, _, body = self.get(f"/media/{file_id}", token=self.viewer.value,
                                   headers={"Range": "bytes=10000-"})
        self.assertEqual(status, 206)
        self.assertEqual(body, data[10000:])

    def test_problem_body_is_json_with_a_code(self):
        status, headers, body = self.get("/media/abc", token=self.viewer.value)
        self.assertEqual(status, 404)
        self.assertEqual(headers.get("Content-Type"), "application/json; charset=utf-8")
        payload = self.json_body(body)
        self.assertTrue(payload.get("error"), "отказ без подписи - молчание")
        self.assertEqual(payload.get("code"), "missing")

    def test_media_bad_range_is_416_not_whole_file(self):
        """Игнорировать чужой Range было бы «отдали не то, что просили»."""
        vid = self.video("media-5", "дорожка")
        data = bytes(range(256)) * 64
        file_id = self.file(vid, "clip.mp4", data, "video")
        for header in ("bytes=abc", "bytes=", "bytes=0-0-", "bytes=5-2",
                       "bytes=999999-", "bytes=0-100,200-300"):
            status, _, body = self.get(f"/media/{file_id}", token=self.viewer.value,
                                       headers={"Range": header})
            self.assertEqual(status, 416, f"{header}: битый Range принят молча")
            payload = self.json_body(body)
            self.assertEqual(payload.get("code"), "range")
            self.assertIn(i18n.t("http.range", "ru"), payload.get("error", ""))
            # Тело - только подпись отказа: байтов дорожки в 416 быть не может.
            self.assertTrue(body.startswith(b"{"),
                            f"{header}: 416 отправил данные файла вместо причины")

    def test_media_head_answers_headers_without_body(self):
        vid = self.video("media-6", "дорожка")
        data = bytes(range(256)) * 64
        file_id = self.file(vid, "clip.mp4", data, "video")
        status, headers, body = self.get(f"/media/{file_id}", token=self.viewer.value,
                                         method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Length"), str(len(data)))
        self.assertEqual(headers.get("Accept-Ranges"), "bytes")
        self.assertEqual(body, b"", "HEAD с телом - это не HEAD")

    def test_media_needs_watch_scope(self):
        vid = self.video("media-7", "дорожка")
        file_id = self.file(vid, "clip.mp4", b"0123456789", "video")
        status, _, body = self.get(f"/media/{file_id}", token=self.card.value)
        self.assertEqual(status, 403)
        self.assertEqual(self.json_body(body).get("scope"), "watch",
                         "байты медиа - право watch (§4), card-ключ его не имеет")

    def test_media_gone_is_named_as_gone(self):
        vid = self.video("media-8", "дорожка пропала")
        file_id = self.file(vid, "clip.mp4", b"0123456789", "video")
        (self.dir / "clip.mp4").unlink()
        status, _, body = self.get(f"/media/{file_id}", token=self.viewer.value)
        self.assertEqual(status, 404)
        self.assertIn("пропал с диска", self.json_body(body).get("error", ""))

    def test_media_unknown_id_is_named(self):
        status, _, body = self.get("/media/999999", token=self.viewer.value)
        self.assertEqual(status, 404)
        self.assertEqual(self.json_body(body).get("code"), "missing")
        self.assertIn("не найден", self.json_body(body).get("error", ""))

    def test_media_wrong_kind_is_not_served(self):
        """Обложку и субтитры /media не отдаёт: id знает свою роль."""
        vid = self.video("media-9", "обложка не дорожка")
        file_id = self.file(vid, "cover.webp", PNG, "thumbnail")
        status, _, body = self.get(f"/media/{file_id}", token=self.viewer.value)
        self.assertEqual(status, 404)
        self.assertIn("не найден", self.json_body(body).get("error", ""))

    def test_viewer_limit_is_enforced_honestly(self):
        """Сверх `serve_watchers_max` - честный 429, полоса не выдумывается."""
        self.assertEqual(self.server._viewers.limit, 1)
        vid = self.video("media-10", "большая дорожка")
        file_id = self.file(vid, "big.mp4", b"\x00" * (3 * 1024 * 1024), "video")
        held = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        tls = self.ctx.wrap_socket(held, server_hostname="localhost")
        tls.sendall(f"GET /media/{file_id} HTTP/1.1\r\nHost: localhost\r\n"
                    f"Authorization: Bearer {self.viewer.value}\r\n\r\n".encode())
        # Клиент ничего не читает: буферы переполняются, обработчик висит в
        # write - зритель занят (раздача держит слот до конца раздачи).
        self.assertTrue(wait_until(lambda: self.server._viewers.count() == 1,
                                   timeout=6.0, message="раздача не держала зрителя"),
                        "раздача не держала зрителя - лимит проверялся бы на пустом месте")
        status, _, body = self.get(f"/media/{file_id}", token=self.viewer.value)
        self.assertEqual(status, 429, "второй зритель принят без лимита")
        payload = self.json_body(body)
        self.assertEqual(payload.get("code"), "rate")
        self.assertIn(i18n.t("http.rate", "ru"), payload.get("error", ""))
        self.assertIn("максимум 1", payload.get("error", ""))
        tls.close()
        self.assertTrue(wait_until(lambda: self.server._viewers.count() == 0,
                                   timeout=6.0, message="зритель не освобождён"),
                        "закрытое соединение оставило зрителя занятым навсегда")
        status, _, body = self.get(f"/media/{file_id}", token=self.viewer.value)
        self.assertEqual(status, 200, "после закрытия первой раздачи право не вернулось")
        self.assertEqual(len(body), 3 * 1024 * 1024)

    # -- субтитры -----------------------------------------------------------

    def test_subs_vtt_served_as_vtt(self):
        vid = self.video("subs-1", "субтитры")
        vtt = "WEBVTT\n\n00:00.000 --> 00:01.000\nпривет\n"
        file_id = self.file(vid, "clip.ru.vtt", vtt.encode("utf-8"), "subtitle")
        status, headers, body = self.get(f"/subs/{file_id}", token=self.viewer.value)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "text/vtt")
        self.assertEqual(body.decode("utf-8"), vtt)

    def test_subs_srt_converts_to_vtt(self):
        vid = self.video("subs-2", "srt")
        srt = "1\n00:00:01,000 --> 00:00:04,500\nпервая строка\n\n2\n" \
              "00:00:05,000 --> 00:00:07,000\nвторая строка\n"
        file_id = self.file(vid, "clip.ru.srt", srt.encode("utf-8"), "subtitle")
        status, _, body = self.get(f"/subs/{file_id}", token=self.viewer.value)
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertTrue(text.startswith("WEBVTT"), "SRT отдан без заголовка VTT")
        self.assertIn("00:00:01.000 --> 00:00:04.500", text,
                      "метка времени SRT не переведена на формат VTT")
        self.assertIn("первая строка", text)
        self.assertIn("вторая строка", text)
        self.assertNotIn("\n1\n", text, "номер cue из SRT остался в треке")

    def test_subs_missing_is_named_not_empty_track(self):
        vid = self.video("subs-3", "без субтитров")
        status, _, body = self.get("/subs/424242", token=self.viewer.value)
        self.assertEqual(status, 404)
        payload = self.json_body(body)
        self.assertIn("не найден", payload.get("error", ""),
                      "пустой трек вместо подписи - плеер молчит и не объясняет")
        self.assertEqual(payload.get("code"), "missing")

    def test_subs_other_format_is_refused_by_name(self):
        vid = self.video("subs-4", "ass")
        file_id = self.file(vid, "clip.ru.ass", b"[Script Info]\n", "subtitle")
        status, _, body = self.get(f"/subs/{file_id}", token=self.viewer.value)
        self.assertEqual(status, 415)
        payload = self.json_body(body)
        self.assertEqual(payload.get("code"), "subs_format")
        self.assertIn(".ass", payload.get("error", ""),
                      "отказ должен называть формат: браузер читает только VTT")

    def test_subs_needs_watch_scope(self):
        vid = self.video("subs-5", "субтитры")
        file_id = self.file(vid, "clip.ru.vtt", b"WEBVTT\n", "subtitle")
        status, _, body = self.get(f"/subs/{file_id}", token=self.card.value)
        self.assertEqual(status, 403)
        self.assertEqual(self.json_body(body).get("scope"), "watch")

    # -- HLS (S3) ------------------------------------------------------------

    def test_hls_route_reads_only_the_cache_of_this_video(self):
        """Чужое имя и «../» не читаются: путь кеша строится по id строки."""
        vid = self.video("hls-1", "дорожка")
        self.file(vid, "clip.mp4", b"0123456789", "video")
        secret = self.dir / "secret.txt"
        secret.write_text("секрет", encoding="utf-8")
        status, _, body = self.get(f"/hls/{vid}/../../secret.txt",
                                   token=self.viewer.value)
        self.assertEqual(status, 404, self.json_body(body))
        self.assertNotIn("секрет", body.decode("utf-8", errors="replace"))
        # Плейлист пишет ffmpeg как media-playlist: `index.m3u8`, а не
        # `master.m3u8` - чужое имя отказано по подписи, а не молча.
        status, _, body = self.get(f"/hls/{vid}/master.m3u8", token=self.viewer.value)
        self.assertEqual(status, 404)
        payload = self.json_body(body)
        self.assertIn(i18n.t("http.hls_segment", "ru"), payload.get("error", ""),
                      "неизвестное имя кеша должно быть названо, а не 404 вслепую")

    def test_hls_refusal_names_the_reason_without_tools(self):
        """Без ffmpeg/ffprobe или на битом файле - 400 с названной причиной.

        Никакого «не получено» и пустого плейлиста: подпись говорит, какой
        инструмент не найден или почему кодек не прочитан.
        """
        vid = self.video("hls-2", "битая дорожка")
        self.file(vid, "clip.mp4", bytes(range(256)), "video")
        status, _, body = self.get(f"/hls/{vid}/index.m3u8", token=self.viewer.value)
        self.assertEqual(status, 400, self.json_body(body))
        payload = self.json_body(body)
        self.assertEqual(payload.get("code"), "hls")
        error = payload.get("error", "")
        self.assertTrue(error, "отказ без подписи - молчание")
        self.assertTrue(any(marker in error for marker in
                            ("ffmpeg", "ffprobe", "кодек", "не прочитан",
                             "не установлен", "HLS")),
                        f"причина отказа не названа инструментом или кодексом: {error!r}")

    # -- журнал -------------------------------------------------------------

    def test_audit_names_routes_without_paths_and_tokens(self):
        vid = self.video("audit-1", "обложка и байты")
        thumb_id = self.file(vid, "a.webp", PNG, "thumbnail")
        media_id = self.file(vid, "a.mp4", b"0123456789" * 4, "video")
        self.get(f"/thumb/{vid}", token=self.card.value)
        self.get(f"/media/{media_id}", token=self.viewer.value)
        self.get("/subs/12345", token=self.viewer.value)
        blob = "\n".join(self.journal.tail(40))
        self.assertIn("thumb", blob)
        self.assertIn("media", blob)
        self.assertIn("subs", blob)
        self.assertIn("ключ карточка", blob)
        self.assertIn("ключ зритель", blob)
        self.assertIn("право read", blob)
        self.assertIn("право watch", blob)
        for value in (self.card.value, self.viewer.value):
            self.assertNotIn(value, blob)
        self.assertFalse(any(marker in blob for marker in (
            str(self.dir), ".mp4", ".webp", ":\\")),
            f"журнал содержит путь или имя файла: {blob[:200]}")

    # -- форма ответа -------------------------------------------------------

    def test_problem_body_is_json_with_a_code(self):
        status, headers, body = self.get("/media/abc", token=self.viewer.value)
        self.assertEqual(status, 404)
        self.assertEqual(headers.get("Content-Type"), "application/json; charset=utf-8")
        payload = self.json_body(body)
        self.assertTrue(payload.get("error"), "отказ без подписи - молчание")
        self.assertEqual(payload.get("code"), "missing")

    def test_unknown_path_still_404(self):
        status, _, body = self.get("/nope/1", token=self.viewer.value)
        self.assertEqual(status, 404)
        self.assertEqual(self.json_body(body).get("code"), "missing")


class TestMediaHelpers(unittest.TestCase):
    """Разбор Range, ETag и конвертация SRT - проверяются без сети."""

    def test_parse_range_forms(self):
        self.assertEqual(serve_media.parse_range("bytes=0-99", 1000), (0, 99))
        self.assertEqual(serve_media.parse_range("bytes=500-", 1000), (500, 999))
        self.assertEqual(serve_media.parse_range("bytes=-100", 1000), (900, 999))
        self.assertEqual(serve_media.parse_range("bytes=0-99999", 1000), (0, 999),
                         "Range за конец файла обязан быть обрезан, а не 416")
        self.assertIsNone(serve_media.parse_range("bytes=abc", 1000))
        self.assertIsNone(serve_media.parse_range("bytes=", 1000))
        self.assertIsNone(serve_media.parse_range("bytes=5-2", 1000))
        self.assertIsNone(serve_media.parse_range("bytes=-0", 1000))
        self.assertIsNone(serve_media.parse_range("bytes=1000-", 1000))
        self.assertIsNone(serve_media.parse_range("bytes=0-10,20-30", 1000),
                          "многоразовый Range не разбираем молча")
        self.assertIsNone(serve_media.parse_range("items=0-1", 1000))
        self.assertIsNone(serve_media.parse_range(None, 1000))
        self.assertIsNone(serve_media.parse_range("bytes=0-10", 0),
                          "пустой файл: Range не читается - честный 416")

    def test_etag_prefers_hash_then_size_mtime(self):
        self.assertEqual(serve_media.etag_for({"hash": "sha256:" + "cd" * 32,
                                               "size": 10, "mtime": 5}),
                         '"cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd"')
        self.assertEqual(serve_media.etag_for({"hash": "", "size": 10, "mtime": 5}),
                         '"10-5"')
        self.assertEqual(serve_media.etag_for({"hash": None, "size": None, "mtime": None}),
                         '"0-0"')

    def test_srt_to_vtt_keeps_cue_text(self):
        raw = "1\n00:00:00,500 --> 00:00:02,000\nпервая\nвторая\n\n" \
              "2\n00:00:03,000 --> 00:00:04,000\nтретья\n"
        out = serve_media.srt_to_vtt(raw)
        self.assertTrue(out.startswith("WEBVTT"))
        self.assertIn("00:00:00.500 --> 00:00:02.000", out)
        self.assertIn("первая\nвторая", out)
        self.assertIn("третья", out)
        self.assertEqual(out.count("-->"), 2)

    def test_srt_to_vtt_drops_blocks_without_timing(self):
        out = serve_media.srt_to_vtt("мусор без метки\n\n1\n00:00:01,000 --> 00:00:02,000\nok\n")
        self.assertNotIn("мусор", out)
        self.assertIn("ok", out)

    def test_viewers_one_limit_for_slots_and_sessions(self):
        """Раздача держит слот до конца, сессия плеера живёт TTL, лимит один."""
        limit = serve_media.Viewers(2, ttl=10)
        self.assertTrue(limit.hold(["media", 1, 5]))
        self.assertTrue(limit.session(["hls", 2, "127.0.0.1"]))
        self.assertFalse(limit.hold(["media", 3, 7]),
                         "третье место принято при лимите 2")
        # Тот же плеер (видео + адрес) продлевает сессию, а не плодит место:
        # сегменты HLS короткие, и каждый сегмент «новым зрителем» - враньё.
        self.assertTrue(limit.session(["hls", 2, "127.0.0.1"]))
        self.assertEqual(limit.count(), 2)
        limit.release(["media", 1, 5])
        self.assertTrue(limit.session(["hls", 4, "127.0.0.1"]))

    def test_viewer_sessions_expire_by_ttl(self):
        limit = serve_media.Viewers(1, ttl=10)
        self.assertTrue(limit.session(["hls", 1, "a"], now=100))
        self.assertFalse(limit.session(["hls", 2, "b"], now=105),
                         "второй плеер принят, пока первый числится зрителем")
        self.assertTrue(limit.session(["hls", 2, "b"], now=111),
                        "TTL истёк - слот обязан освободиться без ожидания")

    def test_viewer_release_never_goes_negative_and_drop_video_frees_slots(self):
        limit = serve_media.Viewers(2, ttl=300)
        limit.release(["media", 1, 5])       # лишнего release - без минуса
        self.assertEqual(limit.count(), 0)
        self.assertTrue(limit.hold(["media", 9, 3]))
        self.assertTrue(limit.session(["hls", 7, "a"], now=0))
        self.assertEqual(limit.count(), 2)
        limit.drop_video(7)                  # сессия удалённой строки снята
        limit.drop_video(9)                  # и слот раздачи этого видео
        self.assertEqual(limit.count(), 0,
                         "строка удалена из индекса, а зрители её остались")

    def test_viewers_zero_limit_means_no_limit(self):
        limit = serve_media.Viewers(0)
        for index in range(5):
            self.assertTrue(limit.hold(["media", index, index]),
                            "0 должен означать без лимита")
            self.assertTrue(limit.session(["hls", index, "a"], now=0))

    def test_mime_tables_cover_player_formats(self):
        self.assertEqual(serve_media.mime_for("a.mkv", serve_media.MEDIA_MIME, "?"),
                         "video/x-matroska")
        self.assertEqual(serve_media.mime_for("a.webm", serve_media.MEDIA_MIME, "?"),
                         "video/webm")
        self.assertEqual(serve_media.mime_for("a.opus", serve_media.MEDIA_MIME, "?"),
                         "audio/opus")
        self.assertEqual(serve_media.mime_for("a.xyz", serve_media.MEDIA_MIME, "?"), "?",
                         "неизвестный тип отдаётся как есть - плеер сам скажет")

    def test_byte_route_maps_ids(self):
        self.assertEqual(serve_http.byte_route("/media/12"), ("media", "12"))
        self.assertEqual(serve_http.byte_route("/thumb/7"), ("thumb", "7"))
        self.assertEqual(serve_http.byte_route("/subs/9"), ("subs", "9"))
        self.assertEqual(serve_http.byte_route("/hls/5/index.m3u8"),
                         ("hls", "5/index.m3u8"))
        self.assertEqual(serve_http.byte_route("/api/poll"), ("", "/api/poll"))
        self.assertEqual(serve_http.byte_route("/nope"), ("", "/nope"))

    def test_route_signatures_exist_in_both_languages(self):
        for key in ("http.thumb_missing", "http.file_missing", "http.subs_missing",
                    "http.subs_format", "http.range", "http.hls_codec",
                    "http.hls_encoder", "http.hls_no_ffmpeg", "http.hls_probe_failed",
                    "http.hls_failed", "http.hls_busy", "http.hls_segment"):
            self.assertTrue(i18n.has(key), f"{key} нет в словаре")
            self.assertTrue(i18n.t(key, "ru"), f"{key} без русской подписи")
            en = i18n.t(key, "en")
            self.assertTrue(en, f"{key} без английского варианта")
            self.assertFalse(any(char in "абвгдежзийклмнопрстуфхцчшщэюя"
                                 for char in en), f"{key}: en с кириллицей")


if __name__ == "__main__":
    unittest.main()
