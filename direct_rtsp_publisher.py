#!/usr/bin/env python3
"""Publish one framed scrcpy H.264 side stream to a local RTSP gateway.

The bridge's independent MPEG-TS archive sink continues if this process exits.
The daemon restarts this publisher after a gateway outage.
"""

from __future__ import annotations

import argparse
from fractions import Fraction
import logging
import socket
import struct
from urllib.parse import urlsplit

import av


MAX_FRAME = 8 * 1024 * 1024
TIME_BASE = Fraction(1, 1_000_000)
LOG = logging.getLogger("direct_rtsp_publisher")


def exact(sock: socket.socket, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        part = sock.recv(size - len(result))
        if not part:
            raise EOFError("raw scrcpy socket closed")
        result.extend(part)
    return bytes(result)


def validate_rtsp(url: str) -> str:
    parsed = urlsplit(url)
    if (parsed.scheme != "rtsp" or parsed.hostname not in ("127.0.0.1", "localhost")
            or parsed.username or parsed.password or not parsed.path.startswith("/")
            or parsed.query or parsed.fragment):
        raise ValueError("RTSP publisher must target local gateway")
    return url


def codec_config(frame: bytes) -> bytes:
    """Extract Annex-B SPS/PPS so RTSP SDP advertises decodable H.264."""
    start = b"\x00\x00\x00\x01"
    pieces = frame.split(start)
    params = [start + piece for piece in pieces if piece and (piece[0] & 31) in (7, 8)]
    if {piece[4] & 31 for piece in params} != {7, 8}:
        raise ValueError("keyframe lacks H.264 SPS/PPS")
    return b"".join(params)


def publish(sock: socket.socket, rtsp_url: str) -> int:
    header = exact(sock, 8)
    if header[:4] != b"SCV1":
        raise ValueError("unsupported raw video protocol")
    width, height = struct.unpack(">HH", header[4:])
    if not 1 <= width <= 8192 or not 1 <= height <= 8192:
        raise ValueError("invalid video size")
    output = stream = None
    first_pts = last_pts = None
    packets = 0
    try:
        while True:
            key, pts, size = struct.unpack(">BQI", exact(sock, 13))
            if key not in (0, 1) or not 0 < size <= MAX_FRAME:
                raise ValueError("invalid raw video packet")
            data = exact(sock, size)
            if output is None:
                if not key:
                    continue
                output = av.open(validate_rtsp(rtsp_url), mode="w", format="rtsp",
                                 options={"rtsp_transport": "tcp", "flush_packets": "1"})
                stream = output.add_stream("h264", rate=30)
                stream.width, stream.height = width, height
                stream.codec_context.extradata = codec_config(data)
                first_pts = pts
            assert stream is not None and first_pts is not None
            next_pts = max((last_pts or -1) + 1, pts - first_pts)
            packet = av.Packet(data)
            packet.stream = stream
            packet.pts = packet.dts = next_pts
            packet.time_base = TIME_BASE
            packet.is_keyframe = bool(key)
            output.mux(packet)
            last_pts = next_pts
            packets += 1
            if packets == 1 or packets % 300 == 0:
                LOG.info("published %d H.264 packets to %s", packets, rtsp_url)
    except EOFError:
        return packets
    finally:
        if output:
            output.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--rtsp-url", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    validate_rtsp(args.rtsp_url)
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(20)
        sock.connect(args.socket)
        sock.settimeout(None)
        publish(sock, args.rtsp_url)


if __name__ == "__main__":
    main()
