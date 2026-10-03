import argparse
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from phone_capture import PhoneWorker, is_archive_path, thermal_state


class ArchiveTests(unittest.TestCase):
    def test_thermal_guard_pauses_on_overheat_and_waits_for_cooldown(self):
        self.assertTrue(thermal_state(3, 45.7, False))
        self.assertFalse(thermal_state(2, 44.9, False))
        self.assertFalse(thermal_state(2, 45.1, False))
        self.assertTrue(thermal_state(2, 50.0, False))
        self.assertTrue(thermal_state(2, 48.5, True))
        self.assertFalse(thermal_state(2, 47.9, True))
        self.assertTrue(thermal_state(None, None, False))
        self.assertFalse(thermal_state(None, 44.9, False))
        self.assertTrue(thermal_state(4, 40.0, False))

    def test_only_generated_phone_clips_are_prunable(self):
        self.assertTrue(is_archive_path("/sdcard/Movies/PhoneCapture/abc123/1791044403.mp4"))
        self.assertTrue(is_archive_path("/sdcard/Movies/PhoneCapture/abc123/20261003T162000Z-1791044403.mp4"))
        self.assertTrue(is_archive_path("/sdcard/Movies/PhoneCapture/abc123/20261003T162000Z-a1b2c3.mp4"))
        self.assertFalse(is_archive_path("/sdcard/Movies/PhoneCapture/abc123/other.mp4"))
        self.assertFalse(is_archive_path("/sdcard/Movies/Camera/abc123/20261003T162000Z-a1b2c3.mp4"))
        self.assertFalse(is_archive_path("/sdcard/Movies/PhoneCapture/../Camera/20261003T162000Z-a1b2c3.mp4"))

    def test_prune_uses_phone_capacity_percent_and_oldest_first(self):
        worker = object.__new__(PhoneWorker)
        worker.device_id = "pixel-4"
        worker.serial = "test-serial"
        worker.socket = None
        worker.paused_hot = False
        worker.config = argparse.Namespace()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        worker.root = Path(temp.name)
        removed = []
        requests = []
        root = "/sdcard/Movies/PhoneCapture/abc123/"
        old = root + "1791044403.mp4"
        new = root + "1791044404.mp4"
        def fake_adb(*args, **_kwargs):
            if args[1] == "df":
                return SimpleNamespace(returncode=0, stdout="Filesystem 1K-blocks Used Available Use% Mounted on\n/dev/fuse 10000000 1000000 9000000 10% /storage/emulated\n")
            if args[1] == "du":
                return SimpleNamespace(returncode=0, stdout=f"4000000\t{old}\n4000000\t{new}\n")
            if args[1] == "dumpsys":
                return SimpleNamespace(returncode=0, stdout="level: 80\ntemperature: 447\n")
            if args[1] == "rm":
                removed.append(args[2])
                return SimpleNamespace(returncode=0)
            raise AssertionError(args)
        worker.adb = fake_adb
        def fake_request(method, path, data=None):
            requests.append((method, path, data))
            return {"retention_mode": "percent", "retention_value": 50}
        worker.request = fake_request
        worker.prune_phone()
        self.assertEqual(removed, [old])
        prune_posts = [req for req in requests if req[1].endswith("/archive-prune")]
        self.assertEqual(len(prune_posts), 1)
        self.assertIn(b'"segment_keys": ["1791044403"]', prune_posts[0][2])

    def test_missing_host_clip_is_recovered_from_phone_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            worker = object.__new__(PhoneWorker)
            worker.root = Path(temp)
            worker.device_id = "pixel-4"
            worker.session_id = None
            worker.mux_process = None
            folder = worker.root / "abc123"
            folder.mkdir()
            marker = folder / "1791044403.mirrored"
            remote = "/sdcard/Movies/PhoneCapture/abc123/20261003T162000Z-1791044403.mp4"
            marker.write_text(remote)
            seen = []
            worker.request = lambda *_args, **_kwargs: {}
            def fake_adb(*args, **_kwargs):
                self.assertEqual(args[:2], ("pull", remote))
                Path(args[2]).write_bytes(b"video")
                return SimpleNamespace(returncode=0)
            worker.adb = fake_adb
            worker.upload_one = lambda sid, clip: seen.append((sid, clip.name)) or True
            worker.upload_pending()
            self.assertEqual(seen, [("abc123", "1791044403.mp4")])


if __name__ == "__main__":
    unittest.main()
