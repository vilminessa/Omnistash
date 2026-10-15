"""Чистка query-мусора в метаданных до подстановки в шаблон имени.

Живой случай: generic-извлечение отдаёт title с хвостом CDN-параметров
(«generic video #?srcIp=…&pr=40&expires=…»), путь из шаблона рос за
MAX_PATH (260) и Windows отдавала Errno 22 даже на валидных именах.
Чистка идёт стадией pre_process (MetadataParserPP), ДО prepare_filename;
id в ИМЕНИ файла дополнительно ограничено лимитом (в БД уходит полный id).
"""

import tempfile
import threading
import unittest
from pathlib import Path

from app import downloader
from yt_dlp import YoutubeDL

JUNK_TITLE = ("generic video #?srcIp=104.28.236.96&pr=40"
              "&expires=1792558867851&srcAg=CHROME&ms=95.163.35.211"
              "&type=6&subId=19944672528967&sig=AaqtUMjElTc&ct=0"
              "&urls=178.237.23")
JUNK_ID = ("?srcIp=104.28.236.96&pr=40&expires=1792558867851"
           "&sig=AaqtUMjElTc&urls=178.237.23&subId=19944672528967") * 3
TEMPLATE = "%(channel)s/%(upload_date)s - %(title)s [%(id)s].%(ext)s"


class JunkMetaCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.opts = downloader.build_opts(
            {"retries": 1, "output_template": TEMPLATE},
            Path(self._tmp.name), stop=threading.Event())

    def fresh_info(self, **over):
        info = {"title": JUNK_TITLE, "channel": "NA", "upload_date": "20261015",
                "id": JUNK_ID, "ext": "mp4",
                "webpage_url": "https://example.com/v1"}
        info.update(over)
        return info

    def run_pipeline(self, info):
        """Как в жизни: сначала pre_process (чистка), потом имя файла.

        pre_process мутирует КОПИЮ и возвращает её - работаем с возвратом,
        как process_ie_result.
        """
        ydl = YoutubeDL({**self.opts})
        info, _ = ydl.pre_process(info)
        return info, ydl.prepare_filename(info)


class TestOptsShape(JunkMetaCase):
    def test_cleaner_registered(self):
        configs = [c for c in self.opts.get("postprocessors", [])
                   if c.get("key") == "MetadataParser"]
        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0]["when"], "pre_process")
        fields = [action[1] for action in configs[0]["actions"]]
        self.assertIn("title", fields)
        self.assertIn("channel", fields)

    def test_id_field_is_length_limited(self):
        self.assertIn(f"%(id).{downloader._ID_FIELD_LIMIT}B", self.opts["outtmpl"])
        self.assertNotIn("%(id)s", self.opts["outtmpl"],
                         "лимит должен применяться ко всем %(id)s-вхождениям")

    def test_user_template_kept_as_is(self):
        self.assertIn("%(title)s", self.opts["outtmpl"],
                      "title не усекаем жёстким капом - только чистим от "
                      "query-мусора, легитимные длинные названия целы")


class TestCleaning(JunkMetaCase):
    def test_junk_title_stripped_to_readable(self):
        info, _ = self.run_pipeline(self.fresh_info())
        self.assertEqual(info["title"], "generic video",
                         "хвост CDN-параметров вырезан")

    def test_legit_titles_untouched(self):
        legit = (
            "Почему так? Разбор = шедевр",
            "Лучшие моменты #хит-парад 2026",
            "Song? feat=X",
            "Просто вопрос?",
            "Купон #скидка50",
        )
        for title in legit:
            info, _ = self.run_pipeline(
                self.fresh_info(title=title, id="dQw4w9WgXcQ"))
            self.assertEqual(info["title"], title,
                             f"легитимный тайтл должен остаться: {title}")

    def test_path_under_max_path(self):
        _, name = self.run_pipeline(self.fresh_info())
        self.assertLessEqual(len(name), 260,
                             "путь обязан влезать в MAX_PATH Windows")
        self.assertNotIn("#?srcIp", name, "query-мусор в имени вычищен")
        self.assertTrue(name.endswith(".mp4"))
        self.assertIn("generic video", name)

    def test_id_capped_in_name_but_full_in_info(self):
        info, name = self.run_pipeline(self.fresh_info())
        self.assertEqual(info["id"], JUNK_ID, "в info/БД уходит полный id")
        stem = Path(name).stem
        bracket = stem.rsplit("[", 1)
        self.assertEqual(len(bracket), 2, "часть [id] в имени сохранена")
        self.assertLessEqual(
            len(bracket[1].rstrip("]")), downloader._ID_FIELD_LIMIT,
            "id в имени ограничен лимитом")
        self.assertLessEqual(len(Path(name).name), 255,
                             "компонент имени в пределах NTFS")


if __name__ == "__main__":
    unittest.main()
