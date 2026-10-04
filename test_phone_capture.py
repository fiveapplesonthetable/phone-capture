import argparse
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from phone_capture import (PhoneWorker, is_archive_path, scrcpy_bridge_command,
                           scrcpy_direct_rtsp_command, scrcpy_ffmpeg_command,
                           scrcpy_live_ffmpeg_command, rtsp_path_available,
                           scrcpy_start_allowed, scrcpy_tunnel_host, stop_workers,
                           thermal_state, validate_local_rtsp)


class ArchiveTests(unittest.TestCase):
    def test_direct_side_commands_preserve_legacy_publisher(self):
        bridge = scrcpy_bridge_command("/tmp/bridge.py", "pixel-4-xl", "TEST", None,
                                       raw_live_socket=True)
        self.assertIn("--raw-live-socket", bridge)
        legacy = scrcpy_live_ffmpeg_command("ffmpeg", "/tmp/live.sock",
                                             "rtsp://127.0.0.1:18554/pixel-4-xl")
        direct = scrcpy_direct_rtsp_command("/tmp/direct.py", "/tmp/raw.sock",
                                           "rtsp://127.0.0.1:18555/pixel-4-xl-direct")
        self.assertIn("18554/pixel-4-xl", legacy[-1])
        self.assertEqual(direct[-1], "rtsp://127.0.0.1:18555/pixel-4-xl-direct")

    def test_webcodecs_side_socket_is_device_opt_in(self):
        normal = scrcpy_bridge_command("/tmp/bridge.py", "pixel-4", "TEST", None)
        enabled = scrcpy_bridge_command("/tmp/bridge.py", "cuttlefish", "TEST", None,
                                        webcodecs_socket=True)
        self.assertNotIn("--webcodecs-socket", normal)
        self.assertIn("--webcodecs-socket", enabled)

    def test_direct_gateway_path_probe_detects_eviction_and_outage(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(2)
        port = listener.getsockname()[1]
        def serve():
            with listener:
                for status in (b"200 OK", b"404 Not Found"):
                    conn, _ = listener.accept()
                    with conn:
                        request = conn.recv(1024)
                        self.assertIn(b"DESCRIBE rtsp://127.0.0.1", request)
                        conn.sendall(b"RTSP/1.0 " + status + b"\r\nCSeq: 1\r\n\r\n")
        server = threading.Thread(target=serve)
        server.start()
        url = f"rtsp://127.0.0.1:{port}/phone-direct"
        self.assertTrue(rtsp_path_available(url))
        self.assertFalse(rtsp_path_available(url))
        server.join(timeout=2)
        self.assertFalse(rtsp_path_available(url))

    def test_cuttlefish_uses_stable_viewer_id_on_local_adb(self):
        worker = object.__new__(PhoneWorker)
        worker.serial = "0.0.0.0:6520"
        worker.socket = None
        worker.adb = lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout="Cuttlefish x86_64 phone\n")
        self.assertTrue(worker.identify())
        self.assertEqual(worker.device_id, "cuttlefish")
        command = scrcpy_bridge_command("/tmp/scrcpy_bridge.py", worker.device_id,
                                        worker.serial, worker.socket,
                                        allow_video_refresh=True)
        self.assertIn("cuttlefish", command)
        self.assertNotIn("--adb-server-socket", command)
        self.assertIn("--allow-video-refresh", command)

    def test_remote_tunnel_and_cool_start_are_device_independent(self):
        self.assertEqual(scrcpy_tunnel_host("tcp:192.0.2.10:5037"), "192.0.2.10")
        with self.assertRaises(ValueError):
            scrcpy_tunnel_host("localabstract:adb")
        self.assertTrue(scrcpy_start_allowed(2, 44.9, 45.0))
        self.assertFalse(scrcpy_start_allowed(3, 40.0, 45.0))
        self.assertFalse(scrcpy_start_allowed(2, 45.0, 45.0))
        self.assertEqual(validate_local_rtsp("rtsp://127.0.0.1:18554/pixel-4"),
                         "rtsp://127.0.0.1:18554/pixel-4")
        with self.assertRaises(ValueError):
            validate_local_rtsp("rtsp://192.0.2.10:18554/pixel-4")
        command = scrcpy_bridge_command("/tmp/scrcpy_bridge.py", "pixel-4-xl",
                                        "TEST123", "tcp:192.0.2.10:5037")
        self.assertEqual(command[6:10], ["--adb-server-socket", "tcp:192.0.2.10:5037",
                                         "--tunnel-host", "192.0.2.10"])
        self.assertIn("4000000", command)
        self.assertNotIn("--allow-video-refresh", command)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg unavailable")
    def test_scrcpy_mkv_stream_yields_playable_archive_clips(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            source = folder / "scrcpy.mkv"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                            "-i", "testsrc2=size=320x480:rate=10:duration=7",
                            "-c:v", "libx264", "-g", "10", str(source)], check=True)
            command = scrcpy_ffmpeg_command("ffmpeg", str(source),
                                            str(folder / "%010d.mp4"), 3, 100)
            subprocess.run(command, check=True, capture_output=True)
            clips = sorted(folder.glob("*.mp4"))
            self.assertGreaterEqual(len(clips), 2)
            for clip in clips:
                result = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                         "stream=codec_name", "-of", "default=nw=1:nk=1",
                                         str(clip)], check=True, capture_output=True, text=True)
                self.assertIn("h264", result.stdout)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg unavailable")
    def test_bridge_mpegts_unix_stream_yields_archive_clips(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            source = folder / "bridge.ts"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                            "-i", "testsrc2=size=320x480:rate=10:duration=6",
                            "-c:v", "libx264", "-g", "10", "-f", "mpegts", str(source)],
                           check=True)
            socket_path = folder / "video.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(socket_path))
            listener.listen(1)
            def serve():
                with listener:
                    connection, _ = listener.accept()
                    with connection:
                        connection.sendall(source.read_bytes())
            server = threading.Thread(target=serve)
            server.start()
            try:
                command = scrcpy_ffmpeg_command("ffmpeg", "unix://" + str(socket_path),
                                                str(folder / "%010d.mp4"), 3, 200,
                                                mpegts_input=True)
                subprocess.run(command, check=True, capture_output=True, timeout=20)
            finally:
                server.join(timeout=5)
            self.assertGreaterEqual(len(list(folder.glob("*.mp4"))), 2)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg unavailable")
    def test_live_gateway_failure_does_not_stop_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            source = folder / "bridge.ts"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                            "-i", "testsrc2=size=320x480:rate=10:duration=6",
                            "-c:v", "libx264", "-g", "10", "-f", "mpegts", str(source)],
                           check=True)
            listeners = []
            servers = []
            for name in ("archive", "live"):
                path = folder / f"{name}.sock"
                listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                listener.bind(str(path))
                listener.listen(1)
                listeners.append(listener)
                def serve(sock=listener):
                    with sock:
                        connection, _ = sock.accept()
                        with connection:
                            connection.sendall(source.read_bytes())
                server = threading.Thread(target=serve)
                server.start()
                servers.append(server)
            archive = scrcpy_ffmpeg_command("ffmpeg", "unix://" + str(folder / "archive.sock"),
                                            str(folder / "%010d.mp4"), 3, 200,
                                            mpegts_input=True)
            live = scrcpy_live_ffmpeg_command("ffmpeg", str(folder / "live.sock"),
                                               "rtsp://127.0.0.1:1/test")
            self.assertEqual(live[live.index("-c:v") + 1], "copy")
            live_proc = subprocess.Popen(live, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)
            try:
                subprocess.run(archive, check=True, capture_output=True, timeout=20)
                self.assertNotEqual(live_proc.wait(timeout=10), 0)
            finally:
                if live_proc.poll() is None:
                    live_proc.kill()
                    live_proc.wait(timeout=5)
                for server in servers:
                    server.join(timeout=5)
            clips = sorted(folder.glob("*.mp4"))
            self.assertGreaterEqual(len(clips), 2)
            subprocess.run(["ffprobe", "-v", "error", str(clips[-1])],
                           check=True, capture_output=True)

    def test_shutdown_skips_uploader_for_unidentified_device(self):
        worker = SimpleNamespace(stop=threading.Event(), thread=threading.Thread(),
                                 uploader=threading.Thread(), device_id=None,
                                 upload_pending=lambda: self.fail("unidentified worker flushed"))
        stop_workers({"missing-phone": worker}, 5)
        self.assertTrue(worker.stop.is_set())

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

    def test_remote_adb_bridge_clip_is_mirrored_before_viewer_upload(self):
        with tempfile.TemporaryDirectory() as temp:
            worker = object.__new__(PhoneWorker)
            worker.socket = "tcp:192.0.2.10:5037"
            worker.device_id = "pixel-4-xl"
            worker.config = argparse.Namespace(segment_seconds=5)
            worker.retry_after = {}
            clip = Path(temp) / "1791044403.mp4"
            clip.write_bytes(b"test clip")
            actions = []
            def fake_adb(*args, **_kwargs):
                actions.append(args[0])
                return SimpleNamespace(returncode=0, stderr="")
            worker.adb = fake_adb
            def fake_request(method, path, data, content_type):
                self.assertTrue(clip.with_suffix(".mirrored").exists())
                self.assertIn("remote_path=%2Fsdcard%2FMovies%2FPhoneCapture", path)
                actions.append("upload")
                return {}
            worker.request = fake_request
            self.assertTrue(worker.upload_one("abc123", clip))
            self.assertEqual(actions, ["shell", "push", "upload"])
            self.assertFalse(clip.exists())


if __name__ == "__main__":
    unittest.main()
