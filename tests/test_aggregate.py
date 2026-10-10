"""Агрегат папки (.omnistash.json): запись, чтение, слияние, переезды.

Один файл на папку вместо пофайловых сайдкаров - поэтому здесь важны
именно свойства «общего файла»: слияние записей, атомарность, устойчивость
к битому содержимому и перенос записи при переезде видео.
"""

import json
import tempfile
import unittest
from pathlib import Path

from app import aggregate


class AggregateCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)

    def record(self, vid, name="Ролик.mp4", digest="sha256:aa"):
        return {"omnistash": 1, "platform": "youtube", "remote_id": vid,
                "path": name, "size": 10, "hash": digest,
                "created_at": "2026-01-01T00:00:00",
                "info": {"id": vid, "title": "Ролик"}}

    def file(self):
        return self.folder / aggregate.FILENAME


class TestWriteRead(AggregateCase):
    def test_records_merge_into_single_file(self):
        aggregate.write_record(self.folder, "aaaaaaaaaaa",
                               self.record("aaaaaaaaaaa"))
        aggregate.write_record(self.folder, "bbbbbbbbbbb",
                               self.record("bbbbbbbbbbb", name="Другоe.mp4"))
        videos = aggregate.read(self.folder)
        self.assertEqual(set(videos), {"aaaaaaaaaaa", "bbbbbbbbbbb"})
        self.assertTrue(self.file().exists())
        # Больше в папке ничего не появилось: пофайловых сайдкаров нет.
        extra = [p.name for p in self.folder.iterdir()
                 if p.name != aggregate.FILENAME]
        self.assertEqual(extra, [])

    def test_find_returns_none_for_unknown(self):
        aggregate.write_record(self.folder, "aaaaaaaaaaa",
                               self.record("aaaaaaaaaaa"))
        self.assertIsNone(aggregate.find(self.folder, "zzzzzzzzzzz"))
        self.assertIsNone(aggregate.find(self.folder, "aaaaaaaaaaa2"))

    def test_write_merges_with_foreign_content(self):
        # Файл мог быть создан не нами (ручная правка/чужой инструмент):
        # незнакомые ключи живут, наша запись дописывается.
        self.file().write_text(json.dumps(
            {"note": "чужое", "videos": {"ccccccccccc": self.record("ccccccccccc")}}),
            encoding="utf-8")
        aggregate.write_record(self.folder, "aaaaaaaaaaa",
                               self.record("aaaaaaaaaaa"))
        raw = json.loads(self.file().read_text(encoding="utf-8"))
        self.assertEqual(raw.get("note"), "чужое")
        self.assertEqual(set(raw["videos"]), {"aaaaaaaaaaa", "ccccccccccc"})

    def test_corrupt_file_is_tolerated_and_healed(self):
        self.file().write_text("{это не json", encoding="utf-8")
        self.assertEqual(aggregate.read(self.folder), {})
        # Скан/качалка не должна падать на битом файле - она его лечит.
        aggregate.write_record(self.folder, "aaaaaaaaaaa",
                               self.record("aaaaaaaaaaa"))
        self.assertEqual(set(aggregate.read(self.folder)), {"aaaaaaaaaaa"})

    def test_build_record_uses_basename(self):
        media = self.folder / "Ролик [aaaaaaaaaaa].mp4"
        media.write_bytes(b"12345")
        record = aggregate.build_record(
            {"id": "aaaaaaaaaaa", "title": "Тест"}, media, "sha256:cc")
        self.assertEqual(record["path"], "Ролик [aaaaaaaaaaa].mp4",
                         "path должен быть именем: переезд папки его не меняет")
        self.assertEqual(record["remote_id"], "aaaaaaaaaaa")
        self.assertEqual(record["hash"], "sha256:cc")
        self.assertEqual(record["size"], 5)
        self.assertEqual(record["info"]["title"], "Тест")


class TestDrop(AggregateCase):
    def test_drop_keeps_other_records(self):
        aggregate.write_record(self.folder, "aaaaaaaaaaa",
                               self.record("aaaaaaaaaaa"))
        aggregate.write_record(self.folder, "bbbbbbbbbbb",
                               self.record("bbbbbbbbbbb"))
        aggregate.drop_record(self.folder, "aaaaaaaaaaa")
        self.assertEqual(set(aggregate.read(self.folder)), {"bbbbbbbbbbb"})
        self.assertTrue(self.file().exists())

    def test_drop_last_record_removes_file(self):
        aggregate.write_record(self.folder, "aaaaaaaaaaa",
                               self.record("aaaaaaaaaaa"))
        aggregate.drop_record(self.folder, "aaaaaaaaaaa")
        self.assertFalse(self.file().exists(), "пустой агрегат - мусор")

    def test_drop_unknown_is_noop(self):
        aggregate.drop_record(self.folder, "aaaaaaaaaaa")
        self.assertFalse(self.file().exists())


class TestRelocate(AggregateCase):
    def test_rename_in_place_updates_path(self):
        aggregate.write_record(self.folder, "aaaaaaaaaaa",
                               self.record("aaaaaaaaaaa", name="old.mp4"))
        aggregate.relocate(self.folder, "aaaaaaaaaaa", self.folder,
                           "new.mp4")
        record = aggregate.find(self.folder, "aaaaaaaaaaa")
        self.assertEqual(record["path"], "new.mp4")
        self.assertEqual(len(aggregate.read(self.folder)), 1)

    def test_move_between_folders_transports_record(self):
        src = self.folder / "a"
        dst = self.folder / "b"
        src.mkdir()
        dst.mkdir()
        aggregate.write_record(src, "aaaaaaaaaaa",
                               self.record("aaaaaaaaaaa"))
        aggregate.write_record(src, "bbbbbbbbbbb",
                               self.record("bbbbbbbbbbb"))

        aggregate.relocate(src, "aaaaaaaaaaa", dst, "Ролик.mp4")
        # Запись переехала, чужая осталась, файлы оба на месте.
        self.assertEqual(set(aggregate.read(dst)), {"aaaaaaaaaaa"})
        self.assertEqual(aggregate.find(dst, "aaaaaaaaaaa")["path"],
                         "Ролик.mp4")
        self.assertEqual(set(aggregate.read(src)), {"bbbbbbbbbbb"})

    def test_move_last_record_cleans_source_file(self):
        src = self.folder / "a"
        dst = self.folder / "b"
        src.mkdir()
        dst.mkdir()
        aggregate.write_record(src, "aaaaaaaaaaa",
                               self.record("aaaaaaaaaaa"))
        aggregate.relocate(src, "aaaaaaaaaaa", dst, "Ролик.mp4")
        self.assertFalse((src / aggregate.FILENAME).exists(),
                         "опустевший агрегат должен уйти")

    def test_move_without_record_is_noop(self):
        dst = self.folder / "b"
        dst.mkdir()
        aggregate.relocate(self.folder, "aaaaaaaaaaa", dst, "x.mp4")
        self.assertFalse((dst / aggregate.FILENAME).exists())


class TestCache(AggregateCase):
    def test_cache_reads_once_and_sees_own_writes(self):
        cache = {}
        aggregate.write_record(self.folder, "aaaaaaaaaaa",
                               self.record("aaaaaaaaaaa"), cache=cache)
        first = aggregate.read(self.folder, cache=cache)
        self.assertEqual(set(first), {"aaaaaaaaaaa"})
        # Кэш заполнен: даже если файл удалить, читатель видит свой словарь.
        self.file().unlink()
        self.assertEqual(set(aggregate.read(self.folder, cache=cache)),
                         {"aaaaaaaaaaa"})
        # Запись через тот же кэш обновляет и кэш, и файл.
        aggregate.write_record(self.folder, "bbbbbbbbbbb",
                               self.record("bbbbbbbbbbb"), cache=cache)
        self.assertEqual(set(aggregate.read(self.folder, cache=cache)),
                         {"aaaaaaaaaaa", "bbbbbbbbbbb"})
        self.assertTrue(self.file().exists())


if __name__ == "__main__":
    unittest.main()
