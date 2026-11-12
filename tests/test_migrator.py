"""Tests for migrator.py. Run: python -m unittest discover -s tests -v"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import migrator as mg  # noqa: E402


class TempTree(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.source = self.root / "src" / ".zcode"
        self.target = self.root / "dst" / ".zcode"
        (self.source / "cli" / "db").mkdir(parents=True)
        (self.source / "cli" / "log").mkdir(parents=True)
        (self.source / "cli" / "log" / "zcode.jsonl").write_text("line\n", encoding="utf-8")
        (self.source / "cli" / "db" / "db.sqlite").write_bytes(b"not-really-sqlite")
        (self.source / "cli" / "db" / "db.sqlite-wal").write_bytes(b"wal")

    def tearDown(self) -> None:
        self._tmp.cleanup()


class TestWalkAndStats(TempTree):
    def test_counts_and_bytes(self) -> None:
        expected = sum(
            p.stat().st_size for p in self.source.rglob("*") if p.is_file()
        )
        files, total = mg.dir_stats(self.source)
        self.assertEqual(files, 3)
        self.assertEqual(total, expected)

    def test_missing_dir(self) -> None:
        self.assertEqual(mg.dir_stats(self.root / "nope"), (0, 0))


class TestSync(TempTree):
    def test_copy_then_skip(self) -> None:
        stats = mg.sync_tree(self.source, self.target)
        self.assertEqual(stats.copied_files, 3)
        self.assertEqual(stats.errors, [])
        self.assertTrue((self.target / "cli" / "log" / "zcode.jsonl").exists())

        again = mg.sync_tree(self.source, self.target)
        self.assertEqual(again.copied_files, 0)
        self.assertEqual(again.skipped_files, 3)

    def test_stale_file_updated(self) -> None:
        mg.sync_tree(self.source, self.target)
        src_file = self.source / "cli" / "log" / "zcode.jsonl"
        src_file.write_text("new content\n", encoding="utf-8")
        os.utime(src_file, (1, 1))
        stats = mg.sync_tree(self.source, self.target)
        self.assertEqual(stats.copied_files, 1)
        self.assertEqual(
            (self.target / "cli" / "log" / "zcode.jsonl").read_text(encoding="utf-8"),
            "new content\n",
        )

    def test_exclude_sqlite(self) -> None:
        stats = mg.sync_tree(self.source, self.target, exclude_sqlite=True)
        self.assertFalse((self.target / "cli" / "db" / "db.sqlite").exists())
        self.assertTrue((self.target / "cli" / "log" / "zcode.jsonl").exists())
        self.assertEqual(stats.copied_files, 1)

    def test_cancel(self) -> None:
        stats = mg.sync_tree(self.source, self.target, cancel=lambda: True)
        self.assertTrue(stats.cancelled)
        self.assertEqual(stats.copied_files, 0)


class TestSidecars(TempTree):
    def test_drop_stale_sidecars(self) -> None:
        mg.sync_tree(self.source, self.target)
        removed = mg.drop_stale_sidecars(self.target)
        self.assertEqual(len(removed), 1)
        self.assertFalse((self.target / "cli" / "db" / "db.sqlite-wal").exists())
        self.assertTrue((self.target / "cli" / "db" / "db.sqlite").exists())


class TestVerify(TempTree):
    def test_valid_database(self) -> None:
        db = self.source / "cli" / "db" / "real.sqlite"
        con = sqlite3.connect(db)
        con.execute("create table t(v integer)")
        con.execute("insert into t values (1)")
        con.commit()
        con.close()

        result = mg.verify_sqlite(db)
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(result.tables, 1)

    def test_missing_database(self) -> None:
        result = mg.verify_sqlite(self.root / "nope.sqlite")
        self.assertFalse(result.ok)

    def test_corrupt_database(self) -> None:
        bad = self.root / "bad.sqlite"
        bad.write_bytes(b"x" * 2048)
        result = mg.verify_sqlite(bad)
        self.assertFalse(result.ok)

    def test_verify_target_only_existing(self) -> None:
        db = self.source / "cli" / "db" / "db.sqlite"
        db.unlink()  # replace the placeholder bytes with a real database
        con = sqlite3.connect(db)
        con.execute("create table t(v integer)")
        con.commit()
        con.close()
        mg.sync_tree(self.source, self.target)
        results = mg.verify_target(self.source, self.target)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].ok)


class TestPreflight(TempTree):
    def test_same_drive_rejected(self) -> None:
        other = self.root / "elsewhere"
        result = mg.preflight(self.source, other)
        self.assertFalse(result.ok)
        self.assertTrue(any("同一磁盘" in e for e in result.errors))

    def test_target_inside_source_rejected(self) -> None:
        result = mg.preflight(self.source, self.source / "inner")
        self.assertFalse(result.ok)

    def test_missing_source(self) -> None:
        result = mg.preflight(self.root / "missing", self.root / "out")
        self.assertFalse(result.ok)

    def test_relative_preflight_with_free_space(self) -> None:
        result = mg.preflight(self.source, self.root / "out")
        self.assertTrue(any("同一磁盘" in e for e in result.errors))


class TestRollbackNames(TempTree):
    def test_unique_names(self) -> None:
        first = mg.rollback_path_for(self.source)
        first.mkdir()
        second = mg.rollback_path_for(self.source)
        self.assertNotEqual(first, second)


class TestJunction(TempTree):
    def test_create_and_remove(self) -> None:
        target = self.root / "real-data"
        target.mkdir()
        (target / "marker.txt").write_text("keep", encoding="utf-8")
        link = self.root / "link"
        ok, detail = mg.make_junction(link, target)
        if not ok or not mg.is_junction(link):
            self.skipTest(f"无法创建目录联接（可能需要权限）: {detail}")
        ok, detail = mg.remove_junction(link)
        self.assertTrue(ok, detail)
        self.assertFalse(link.exists())
        self.assertTrue((target / "marker.txt").exists())

    def test_refuse_to_remove_normal_dir(self) -> None:
        ok, _detail = mg.remove_junction(self.source)
        self.assertFalse(ok)
        self.assertTrue(self.source.exists())


class TestStatusShape(TempTree):
    def test_setting_data_base_dir(self) -> None:
        v2 = self.source / "v2"
        v2.mkdir()
        (v2 / "setting.json").write_text('{"dataBaseDir": "E:\\\\ZCODE"}', encoding="utf-8")
        self.assertEqual(mg.read_setting_data_base_dir(self.source), "E:\\ZCODE")

    def test_setting_missing(self) -> None:
        self.assertIsNone(mg.read_setting_data_base_dir(self.source))


if __name__ == "__main__":
    unittest.main()
