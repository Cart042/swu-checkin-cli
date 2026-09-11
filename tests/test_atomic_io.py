import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import atomic_io


class AtomicWriteTextTests(unittest.TestCase):
    def test_creates_private_file_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "nested" / "secret.txt"

            atomic_io.atomic_write_text(destination, "secret\n", prefix=".test-write-")

            self.assertEqual(destination.read_text(encoding="utf-8"), "secret\n")
            self.assertEqual(list(destination.parent.glob(".test-write-*")), [])
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(destination.parent.stat().st_mode), 0o700)

    def test_does_not_chmod_existing_parent_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "existing"
            parent.mkdir()
            if os.name == "nt":
                self.skipTest("Windows does not expose POSIX directory permission bits")
            os.chmod(parent, 0o755)

            atomic_io.atomic_write_text(parent / "secret.txt", "secret")

            self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o755)

    def test_replace_failure_preserves_old_file_and_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "secret.txt"
            destination.write_text("old\n", encoding="utf-8")

            with mock.patch.object(atomic_io.os, "replace", side_effect=OSError("replace failed")):
                with mock.patch.object(atomic_io.os, "close", wraps=os.close) as close:
                    with self.assertRaisesRegex(OSError, "replace failed"):
                        atomic_io.atomic_write_text(
                            destination,
                            "new\n",
                            prefix=".failed-write-",
                        )
                    # fdopen has taken ownership and closed the descriptor;
                    # the exception path must not close its stale integer.
                    close.assert_not_called()

            self.assertEqual(destination.read_text(encoding="utf-8"), "old\n")
            self.assertEqual(list(destination.parent.glob(".failed-write-*")), [])


if __name__ == "__main__":
    unittest.main()
