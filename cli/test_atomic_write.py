import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from atomic_write import write_file_atomically


class AtomicWriteTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.path = self.dir / "file.txt"

    def leftover_files(self):
        return [p.name for p in self.dir.iterdir() if p != self.path]

    def test_writes_new_file(self):
        write_file_atomically(self.path, "hello")
        self.assertEqual(self.path.read_text(), "hello")
        self.assertEqual(self.leftover_files(), [])

    def test_accepts_string_paths(self):
        write_file_atomically(str(self.path), "hello")
        self.assertEqual(self.path.read_text(), "hello")

    def test_refuses_to_overwrite_by_default(self):
        self.path.write_text("original")
        with self.assertRaises(FileExistsError):
            write_file_atomically(self.path, "new")
        self.assertEqual(self.path.read_text(), "original")
        self.assertEqual(self.leftover_files(), [])

    def test_replace_overwrites(self):
        self.path.write_text("original")
        write_file_atomically(self.path, "new", replace=True)
        self.assertEqual(self.path.read_text(), "new")
        self.assertEqual(self.leftover_files(), [])

    def test_replace_creates_missing_file(self):
        write_file_atomically(self.path, "new", replace=True)
        self.assertEqual(self.path.read_text(), "new")

    def test_refuses_to_clobber_file_created_during_write(self):
        # Another writer lands between the existence check and the final
        # step: the no-replace path must still not overwrite it.
        real_link = os.link

        def link_after_competitor(source, target):
            Path(target).write_text("competitor")
            return real_link(source, target)

        with patch("atomic_write.os.link", side_effect=link_after_competitor):
            with self.assertRaises(FileExistsError):
                write_file_atomically(self.path, "new")

        self.assertEqual(self.path.read_text(), "competitor")
        self.assertEqual(self.leftover_files(), [])

    def test_failures_preserve_existing_file(self):
        for patched in ("atomic_write.os.fsync", "atomic_write.os.replace"):
            self.path.write_text("original")
            with self.subTest(patched=patched):
                with patch(patched, side_effect=OSError("boom")):
                    with self.assertRaisesRegex(RuntimeError, "Could not write"):
                        write_file_atomically(self.path, "new", replace=True)
                self.assertEqual(self.path.read_text(), "original")
                self.assertEqual(self.leftover_files(), [])

    def test_failed_write_preserves_existing_file(self):
        self.path.write_text("original")
        with self.assertRaises(TypeError):
            write_file_atomically(self.path, 123, replace=True)  # not text
        self.assertEqual(self.path.read_text(), "original")
        self.assertEqual(self.leftover_files(), [])

    def test_failed_first_write_leaves_nothing(self):
        with patch("atomic_write.os.fsync", side_effect=OSError("boom")):
            with self.assertRaises(RuntimeError):
                write_file_atomically(self.path, "new")
        self.assertFalse(self.path.exists())
        self.assertEqual(self.leftover_files(), [])

    def test_missing_directory_fails_cleanly(self):
        with self.assertRaisesRegex(RuntimeError, "Could not write"):
            write_file_atomically(self.dir / "missing" / "file.txt", "x")

    def test_directory_sync_failure_does_not_fail_the_write(self):
        real_open = os.open

        def open_fails_for_directories(path, flags, *args):
            if flags == os.O_RDONLY:
                raise OSError("cannot open directory")
            return real_open(path, flags, *args)

        with patch("atomic_write.os.open", side_effect=open_fails_for_directories):
            write_file_atomically(self.path, "hello")
        self.assertEqual(self.path.read_text(), "hello")

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_file_is_owner_only(self):
        write_file_atomically(self.path, "secret")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_replace_swaps_symlink_without_writing_through_it(self):
        target = self.dir / "target.txt"
        target.write_text("target")
        self.path.symlink_to(target)

        write_file_atomically(self.path, "new", replace=True)

        self.assertFalse(self.path.is_symlink())
        self.assertEqual(self.path.read_text(), "new")
        self.assertEqual(target.read_text(), "target")

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_dangling_symlink_counts_as_existing(self):
        self.path.symlink_to(self.dir / "nowhere.txt")
        with self.assertRaises(FileExistsError):
            write_file_atomically(self.path, "new")


if __name__ == "__main__":
    unittest.main()
