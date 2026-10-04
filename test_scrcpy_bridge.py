"""Protocol and input validation for the one-encoder scrcpy bridge."""

import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
import shutil

import av

from scrcpy_bridge import (AdbPipe, ScrcpyBridge, VideoSink, exact, key_message,
                           text_message, touch_message, touch_pointer_id)


class ScrcpyProtocolTests(unittest.TestCase):
    def test_idle_video_refresh_is_opt_in_and_rate_limited(self):
        class FakeControl:
            def __init__(self):
                self.sent = []
            def sendall(self, data):
                self.sent.append(data)
        with tempfile.TemporaryDirectory() as temp:
            kwargs = dict(device_id="cuttlefish", serial="TEST", adb_server_socket=None,
                          tunnel_host="127.0.0.1", server_jar=Path(temp)/"server.jar",
                          relay_jar=Path(temp)/"relay.jar", runtime_dir=Path(temp))
            bridge = ScrcpyBridge(**kwargs)
            bridge.control = FakeControl()
            bridge.sinks["live"] = object()
            bridge.last_video_packet_at = time.monotonic() - 3
            with self.assertRaises(ValueError):
                bridge._control_request({"type":"refresh_video"})
            self.assertEqual(bridge.control.sent, [])
            bridge.allow_video_refresh = True
            self.assertTrue(bridge._control_request({"type":"refresh_video"}))
            self.assertFalse(bridge._control_request({"type":"refresh_video"}))
            self.assertEqual(bridge.control.sent, [bytes([17])])
            # A viewer can connect after the cold-start IDR has passed.
            bridge.last_video_refresh_at = time.monotonic() - 1
            self.assertTrue(bridge._control_request({"type":"refresh_video", "phase":"connected"}))
            self.assertFalse(bridge._control_request({"type":"refresh_video", "phase":"connected"}))
            self.assertEqual(bridge.control.sent, [bytes([17]), bytes([17])])
            bridge.last_video_refresh_at = time.monotonic() - 21
            bridge.last_video_packet_at = time.monotonic()
            self.assertFalse(bridge._control_request({"type":"refresh_video"}))
            self.assertEqual(len(bridge.control.sent), 2)
            with self.assertRaises(ValueError):
                bridge._control_request({"type":"refresh_video", "phase":"unlimited"})

    def test_cuttlefish_bridge_id_is_accepted(self):
        with tempfile.TemporaryDirectory() as temp:
            bridge = ScrcpyBridge(device_id="cuttlefish", serial="0.0.0.0:6520",
                                  adb_server_socket=None, tunnel_host="127.0.0.1",
                                  server_jar=Path(temp) / "server.jar",
                                  relay_jar=Path(temp) / "relay.jar",
                                  runtime_dir=Path(temp))
            self.assertEqual(bridge.device_id, "cuttlefish")

    def test_short_keyframe_interval_is_bounded_for_live_join(self):
        with tempfile.TemporaryDirectory() as temp:
            kwargs = dict(device_id="pixel-4-xl", serial="TEST", adb_server_socket=None,
                          tunnel_host="127.0.0.1", server_jar=Path(temp) / "server.jar",
                          relay_jar=Path(temp) / "relay.jar", runtime_dir=Path(temp))
            self.assertEqual(ScrcpyBridge(**kwargs).i_frame_interval, 1)
            for value in (0, 11):
                with self.assertRaises(ValueError):
                    ScrcpyBridge(**kwargs, i_frame_interval=value)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg unavailable")
    def test_archive_overflow_cannot_stop_live_video(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            source = path / "source.ts"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                            "-f", "lavfi", "-i", "testsrc2=size=320x480:rate=15:duration=2",
                            "-c:v", "libx264", "-tune", "zerolatency", "-bf", "0",
                            "-g", "15", "-f", "mpegts", str(source)],
                           check=True, capture_output=True)
            archived, archived_peer = socket.socketpair()
            live, live_peer = socket.socketpair()
            archive_sink = VideoSink("archive", archived, 320, 480, 15, 1, 1)
            live_sink = VideoSink("live", live, 320, 480, 15, 60, 8 * 1024 * 1024)
            received = bytearray()
            def drain_live():
                while True:
                    chunk = live_peer.recv(65536)
                    if not chunk:
                        break
                    received.extend(chunk)
            reader = threading.Thread(target=drain_live, daemon=True)
            reader.start()
            try:
                packets = [p for p in av.open(str(source)).demux(video=0) if p.size]
                self.assertFalse(archive_sink.offer(bytes(packets[0]), 0, True, b""))
                for packet in packets:
                    self.assertTrue(live_sink.offer(bytes(packet),
                                                    int(packet.pts * packet.time_base * 1_000_000),
                                                    packet.is_keyframe, b""))
                deadline = time.monotonic() + 3
                while live_sink.pending_bytes and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(live_sink.pending_bytes, 0)
                live_sink.close()
                reader.join(timeout=3)
                output = path / "live.ts"
                output.write_bytes(received)
                probe = subprocess.run(["ffprobe", "-v", "error", "-count_frames",
                                        "-select_streams", "v:0", "-show_entries",
                                        "stream=nb_read_frames", "-of", "default=nw=1:nk=1",
                                        str(output)], capture_output=True, text=True, check=True)
                self.assertGreaterEqual(int(probe.stdout.strip().splitlines()[-1]), 15)
            finally:
                archive_sink.close()
                live_sink.close()
                archived_peer.close()
                live_peer.close()

    def test_relay_startup_waits_for_connected_marker(self):
        process = subprocess.Popen(["bash", "-c",
                                    "printf 'startup\\nPHONE_CAPTURE_RELAY_READY\\n' >&2; sleep .2"],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        pipe = AdbPipe(process)
        try:
            ScrcpyBridge._wait_relay_connected(pipe)
        finally:
            pipe.close()

    def test_exact_collects_fragmented_binary_data(self):
        sender, receiver = socket.socketpair()
        try:
            sender.sendall(b"\x00\xff\x01\x02")
            self.assertEqual(exact(receiver, 4), b"\x00\xff\x01\x02")
            sender.close()
            with self.assertRaises(EOFError):
                exact(receiver, 1)
        finally:
            receiver.close()

    def test_key_uses_scrcpy_binary_protocol(self):
        self.assertEqual(key_message(4, 0), struct.pack(">BBIII", 0, 0, 4, 0, 0))
        self.assertEqual(key_message(4, 1), struct.pack(">BBIII", 0, 1, 4, 0, 0))

    def test_touch_scales_normalized_coordinates(self):
        message = touch_message(0, 1, 0.5, 304, 720)
        self.assertEqual(struct.unpack(">BBQIIHHHII", message),
                         (2, 0, 0xFFFFFFFFFFFFFFFE, 303, 360, 304, 720, 65535, 0, 0))
        for coordinate in (-0.01, 1.01, float("nan"), True):
            with self.assertRaises(ValueError):
                touch_message(0, coordinate, 0.5, 304, 720)

    def test_two_pointers_use_distinct_scrcpy_ids_and_cancel_releases_both(self):
        class FakeControl:
            def __init__(self):
                self.sent = []
            def sendall(self, data):
                self.sent.append(data)
        with tempfile.TemporaryDirectory() as temp:
            bridge = ScrcpyBridge(device_id="cuttlefish", serial="TEST",
                                  adb_server_socket=None, tunnel_host="127.0.0.1",
                                  server_jar=Path(temp)/"server.jar",
                                  relay_jar=Path(temp)/"relay.jar",
                                  runtime_dir=Path(temp))
            bridge.control = FakeControl()
            bridge.width, bridge.height = 304, 720
            for pointer_id, x in ((0, .3), (1, .7)):
                bridge._control_request({"type": "touch", "action": "down",
                                         "pointer_id": pointer_id, "x": x, "y": .5})
            bridge._control_request({"type": "touch", "action": "move",
                                     "pointer_id": 0, "x": .2, "y": .5, "pressure": .5})
            bridge._control_request({"type": "touch", "action": "move",
                                     "pointer_id": 1, "x": .8, "y": .5})
            bridge._control_request({"type": "touch", "action": "cancel"})
            sent = [struct.unpack(">BBQIIHHHII", msg) for msg in bridge.control.sent]
            self.assertEqual([(msg[1], msg[2]) for msg in sent],
                             [(0, 0), (0, 1), (2, 0), (2, 1), (1, 1), (1, 0)])
            self.assertEqual(sent[2][7], round(.5 * 65535))
            self.assertEqual(bridge.active_pointers, {})
            bridge._control_request({"type": "touch", "action": "up",
                                     "pointer_id": 1, "x": .8, "y": .5})
            self.assertEqual(len(bridge.control.sent), 6)

    def test_touch_sequence_and_pointer_values_are_bounded(self):
        for value in (-1, 10, True, 1.5, "1"):
            with self.assertRaises(ValueError):
                touch_pointer_id(value)
        for pressure in (-.1, 1.1, float("nan")):
            with self.assertRaises(ValueError):
                touch_message(0, .5, .5, 304, 720, 0, pressure)
        with tempfile.TemporaryDirectory() as temp:
            bridge = ScrcpyBridge(device_id="cuttlefish", serial="TEST",
                                  adb_server_socket=None, tunnel_host="127.0.0.1",
                                  server_jar=Path(temp)/"server.jar",
                                  relay_jar=Path(temp)/"relay.jar",
                                  runtime_dir=Path(temp))
            bridge.control = type("FakeControl", (), {"sendall": lambda *_: None})()
            bridge.width, bridge.height = 304, 720
            with self.assertRaisesRegex(ValueError, "active pointer"):
                bridge._control_request({"type": "touch", "action": "move",
                                         "pointer_id": 0, "x": .5, "y": .5})
            bridge._control_request({"type": "touch", "action": "down",
                                     "pointer_id": 0, "x": .5, "y": .5})
            bridge.last_touch_at = time.monotonic() - 20
            # The same release path is used by the idle watchdog and shutdown.
            bridge._cancel_touches()
            self.assertEqual(bridge.active_pointers, {})

    def test_text_is_utf8_and_bounded(self):
        self.assertEqual(text_message("Hi"), b"\x01\x00\x00\x00\x02Hi")
        for value in ("", "bad\ntext", "x" * 201, "😀" * 100):
            with self.assertRaises(ValueError):
                text_message(value)


if __name__ == "__main__":
    unittest.main()
