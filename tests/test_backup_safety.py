"""Safety regression tests for portable backup and restore helpers."""
from __future__ import annotations
import hashlib
import io
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import master


class BackupRestoreSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cctv-backup-test-")
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        (self.root / "USER_CONTROL.py").write_text(
            "BACKUP_DIRECTORY = 'backups'\nRESTORE_CREATE_ROLLBACK_COPY = True\n",
            encoding="utf-8",
        )
        (self.root / "config").mkdir()
        (self.root / "config" / "cameras.json").write_text('{"cameras": []}\n')
        (self.root / "data").mkdir()
        (self.root / "data" / "recording.ts").write_bytes(b"recording")
        (self.root / "cache").mkdir()
        (self.root / "cache" / "cache.bin").write_bytes(b"cache")
        (self.root / "docker" / "data").mkdir(parents=True)
        (self.root / "docker" / "data" / "daemon-layer.bin").write_bytes(b"live docker storage")
        (self.root / "backups").mkdir()
        self.old_root = master.ROOT
        master.ROOT = self.root

    def tearDown(self):
        master.ROOT = self.old_root
        self.temp.cleanup()

    def test_no_cache_backup_contains_data_and_valid_checksum(self):
        master.create_backup(False)
        archives = list((self.root / "backups").glob("*.tar.gz"))
        self.assertEqual(len(archives), 1)
        archive = archives[0]
        sidecar = archive.with_suffix(archive.suffix + ".sha256")
        self.assertTrue(sidecar.is_file())
        self.assertEqual(
            sidecar.read_text().split()[0],
            hashlib.sha256(archive.read_bytes()).hexdigest(),
        )
        with tarfile.open(archive, "r:gz") as source:
            names = set(source.getnames())
        self.assertIn("USER_CONTROL.py", names)
        self.assertIn("data/recording.ts", names)
        self.assertFalse(any(name.startswith("cache/") for name in names))
        self.assertFalse(any(name.startswith("docker/data/") for name in names))

    def test_safe_extract_rejects_parent_traversal(self):
        archive_path = self.root / "unsafe.tar"
        with tarfile.open(archive_path, "w") as archive:
            info = tarfile.TarInfo("../escape")
            payload = b"no"
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        destination = self.root / "extract"
        destination.mkdir()
        with tarfile.open(archive_path, "r") as archive:
            with self.assertRaises(RuntimeError):
                master.safe_extract(archive, destination)
        self.assertFalse((self.root.parent / "escape").exists())

    def test_no_cache_restore_clears_stale_cache(self):
        stage = self.root / "stage"
        stage.mkdir()
        (stage / "USER_CONTROL.py").write_text("BACKUP_DIRECTORY='backups'\n")
        (stage / "config").mkdir()
        (stage / "config" / "cameras.json").write_text('{"cameras": []}')
        (self.root / "cache" / "stale.bin").write_text("stale")
        master.replace_from_stage(stage, clear_cache_if_missing=True)
        self.assertFalse((self.root / "cache").exists())


if __name__ == "__main__":
    unittest.main()
