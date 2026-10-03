"""Framing and isolation checks for the optional raw RTSP side publisher."""

import socket
import struct
import threading
import unittest
from unittest.mock import patch

from direct_rtsp_publisher import codec_config, publish, validate_rtsp
from scrcpy_bridge import RawVideoSink, VideoSink


class DirectRtspTests(unittest.TestCase):
    def test_local_gateway_only(self):
        self.assertEqual(validate_rtsp("rtsp://127.0.0.1:18554/test-direct"),
                         "rtsp://127.0.0.1:18554/test-direct")
        for url in ("rtsp://example.com/test", "http://127.0.0.1/test",
                    "rtsp://user@localhost/test", "rtsp://localhost/test?x=1"):
            with self.assertRaises(ValueError):
                validate_rtsp(url)

    def test_raw_reader_starts_with_fresh_keyframe_and_keeps_archive_independent(self):
        raw_writer, raw_reader = socket.socketpair()
        archive_writer, archive_reader = socket.socketpair()
        raw_sink = RawVideoSink(raw_writer, 720, 1520)
        archive_sink = VideoSink("archive", archive_writer, 720, 1520, 30, 4,
                                 1024 * 1024)
        try:
            self.assertTrue(raw_sink.offer(b"\0\0\0\1\x65", 42, True,
                                           b"\0\0\0\1\x67"))
            self.assertEqual(raw_reader.recv(8), b"SCV1" + struct.pack(">HH", 720, 1520))
            self.assertEqual(raw_reader.recv(13), struct.pack(">BQI", 1, 42, 10))
            self.assertEqual(raw_reader.recv(10),
                             b"\0\0\0\1\x67\0\0\0\1\x65")
            raw_reader.close()
            raw_sink.close()
            self.assertTrue(archive_sink.offer(b"\0\0\0\1\x65", 42, True, b""))
        finally:
            raw_sink.close()
            archive_sink.close()
            archive_reader.close()

    def test_publisher_skips_delta_until_keyframe_and_rescales_pts(self):
        writer, reader = socket.socketpair()
        class Output:
            def __init__(self):
                self.packets = []
                self.closed = False
            def add_stream(self, *args, **kwargs):
                return type("Stream", (), {"codec_context": type("Codec", (), {})()})()
            def mux(self, packet):
                self.packets.append((packet.pts, packet.is_keyframe, bytes(packet)))
            def close(self):
                self.closed = True
        class Packet:
            def __init__(self, data):
                self.data = data
            def __bytes__(self):
                return self.data
        output = Output()
        config = b"\0\0\0\1\x67sps\0\0\0\1\x68pps"
        def feed():
            writer.sendall(b"SCV1" + struct.pack(">HH", 64, 64))
            for key, pts, payload in ((0, 100, b"delta"), (1, 200, config + b"key"),
                                      (0, 250, b"next")):
                writer.sendall(struct.pack(">BQI", key, pts, len(payload)) + payload)
            writer.close()
        thread = threading.Thread(target=feed)
        thread.start()
        try:
            with patch("direct_rtsp_publisher.av.open", return_value=output), \
                    patch("direct_rtsp_publisher.av.Packet", Packet):
                self.assertEqual(publish(reader, "rtsp://localhost:18554/test"), 2)
            self.assertEqual(output.packets, [(0, True, config + b"key"),
                                              (50, False, b"next")])
            self.assertTrue(output.closed)
        finally:
            reader.close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
