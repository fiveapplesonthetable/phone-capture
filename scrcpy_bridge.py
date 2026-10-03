#!/usr/bin/env python3
"""Bridge scrcpy 4.1 video and control sockets to local, per-device sockets.

The video socket serves MPEG-TS with the device encoder's original H.264 PTS.
Only one reader is served at a time. A disconnected reader may reconnect at the
next keyframe; the scrcpy server and its control socket keep running.
"""

from __future__ import annotations

import argparse
import fcntl
from fractions import Fraction
import json
import logging
import math
import os
from pathlib import Path
import queue
import secrets
import select
import signal
import socket
import struct
import subprocess
import threading
import time

import av

LOG = logging.getLogger("scrcpy_bridge")
VERSION = "4.1"
MAX_PACKET = 8 * 1024 * 1024
MAX_REQUEST = 4096
KEYCODES = {
    "BACK": 4, "HOME": 3, "ENTER": 66, "APP_SWITCH": 187,
    "POWER": 26, "VOLUME_UP": 24, "VOLUME_DOWN": 25,
    "BACKSPACE": 67, "ARROW_UP": 19, "ARROW_DOWN": 20,
    "ARROW_LEFT": 21, "ARROW_RIGHT": 22, "TAB": 61,
}


class VideoSink:
    """One bounded, independent MPEG-TS writer for a local FFmpeg consumer."""

    def __init__(self, name: str, reader: socket.socket, width: int, height: int,
                 max_fps: int, max_packets: int, max_bytes: int):
        self.name = name
        self.reader = reader
        self.width, self.height = width, height
        self.max_fps = max_fps
        self.max_bytes = max_bytes
        self.pending_bytes = 0
        self.lock = threading.Lock()
        self.closed = False
        self.items: queue.Queue[tuple[bytes, int, bool, bytes] | None] = queue.Queue(max_packets)
        self.thread = threading.Thread(target=self._write_loop, daemon=True)
        self.thread.start()

    def offer(self, payload: bytes, pts: int, keyframe: bool, config: bytes) -> bool:
        size = len(payload) + (len(config) if keyframe else 0)
        with self.lock:
            if self.closed:
                return False
            if self.pending_bytes + size > self.max_bytes or self.items.full():
                LOG.warning("%s video consumer fell behind; disconnecting", self.name)
                self.close()
                return False
            self.items.put_nowait((payload, pts, keyframe, config))
            self.pending_bytes += size
        return True

    def close(self) -> None:
        # Do not wait for a blocked writer: shutdown wakes its socket write.
        self.closed = True
        try:
            self.reader.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.reader.close()

    def _write_loop(self) -> None:
        muxer = stream = mux_file = None
        pts_base = last_pts = 0
        try:
            mux_file = self.reader.makefile("wb", buffering=0)
            while not self.closed:
                try:
                    item = self.items.get(timeout=.25)
                except queue.Empty:
                    continue
                if item is None:
                    break
                payload, pts, keyframe, config = item
                size = len(payload) + (len(config) if keyframe else 0)
                with self.lock:
                    self.pending_bytes -= size
                if muxer is None:
                    if not keyframe:
                        continue
                    muxer = av.open(mux_file, mode="w", format="mpegts")
                    stream = muxer.add_stream("h264", rate=self.max_fps)
                    stream.width, stream.height = self.width, self.height
                    pts_base = pts
                if keyframe and config:
                    payload = config + payload
                packet = av.Packet(payload)
                packet.stream = stream
                packet.pts = packet.dts = max(last_pts, pts - pts_base)
                packet.time_base = Fraction(1, 1_000_000)
                packet.is_keyframe = keyframe
                muxer.mux(packet)
                last_pts = packet.pts
        except (OSError, ValueError, av.FFmpegError) as exc:
            LOG.info("%s video consumer closed: %s", self.name, exc)
        finally:
            if muxer:
                try:
                    muxer.close()
                except (OSError, av.FFmpegError):
                    pass
            if mux_file:
                mux_file.close()
            self.close()


class AdbPipe:
    """Socket-like wrapper around one binary, no-PTY ADB shell channel."""

    def __init__(self, process: subprocess.Popen):
        self.process = process

    def fileno(self) -> int:
        assert self.process.stdout
        return self.process.stdout.fileno()

    def recv(self, count: int) -> bytes:
        return os.read(self.fileno(), count)

    def sendall(self, payload: bytes) -> None:
        assert self.process.stdin
        view = memoryview(payload)
        while view:
            written = os.write(self.process.stdin.fileno(), view)
            view = view[written:]

    def close(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=2)
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
            if pipe:
                pipe.close()


def exact(sock: socket.socket | AdbPipe, count: int) -> bytes:
    result = bytearray()
    while len(result) < count:
        part = sock.recv(count - len(result))
        if not part:
            raise EOFError("scrcpy socket closed")
        result.extend(part)
    return bytes(result)


def number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError("coordinate must be a number from 0 to 1")
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("coordinate must be a number from 0 to 1")
    return value


def key_message(keycode: int, action: int) -> bytes:
    return struct.pack(">BBIII", 0, action, keycode, 0, 0)


def touch_message(action: int, x: float, y: float, width: int, height: int) -> bytes:
    if not (1 <= width <= 65535 and 1 <= height <= 65535):
        raise ValueError("video size unavailable")
    px = round(number(x) * (width - 1))
    py = round(number(y) * (height - 1))
    pressure = 0 if action == 1 else 65535
    return struct.pack(">BBQIIHHHII", 2, action, 0xFFFFFFFFFFFFFFFE,
                       px, py, width, height, pressure, 0, 0)


def text_message(text: str) -> bytes:
    if not isinstance(text, str) or not text or len(text) > 200 or any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise ValueError("text must contain 1–200 printable characters")
    encoded = text.encode("utf-8")
    if len(encoded) > 300:
        raise ValueError("text exceeds scrcpy's 300-byte limit")
    return struct.pack(">BI", 1, len(encoded)) + encoded


class ScrcpyBridge:
    def __init__(self, *, device_id: str, serial: str, adb_server_socket: str | None,
                 tunnel_host: str, server_jar: Path, relay_jar: Path, runtime_dir: Path,
                 bit_rate: int = 4_000_000, max_size: int = 720, max_fps: int = 30,
                 i_frame_interval: int = 1):
        if device_id not in ("pixel-4", "pixel-4-xl"):
            raise ValueError("unknown device id")
        if not serial or any(c.isspace() for c in serial):
            raise ValueError("invalid ADB serial")
        self.device_id = device_id
        self.serial = serial
        self.tunnel_host = tunnel_host
        self.server_jar = server_jar
        self.relay_jar = relay_jar
        self.runtime_dir = runtime_dir
        self.bit_rate = bit_rate
        self.max_size = max_size
        self.max_fps = max_fps
        if not 1 <= i_frame_interval <= 10:
            raise ValueError("I-frame interval must be 1–10 seconds")
        self.i_frame_interval = i_frame_interval
        self.env = dict(os.environ)
        if adb_server_socket:
            self.env["ADB_SERVER_SOCKET"] = adb_server_socket
        else:
            self.env.pop("ADB_SERVER_SOCKET", None)
        self.scid = secrets.randbelow(0x7FFFFFFF)
        self.remote_jar = f"/data/local/tmp/phone-capture-scrcpy-{self.scid:08x}.jar"
        self.video_socket_path = runtime_dir / f"{device_id}.video.sock"
        self.live_socket_path = runtime_dir / f"{device_id}.live.sock"
        self.control_socket_path = runtime_dir / f"{device_id}.control.sock"
        self.server_process: subprocess.Popen | None = None
        self.lock_file = None
        self.lock_acquired = False
        self.remote_jar_pushed = False
        self.remote_relay_jar = f"/data/local/tmp/phone-capture-relay-{self.scid:08x}.jar"
        self.video: socket.socket | AdbPipe | None = None
        self.control: socket.socket | AdbPipe | None = None
        self.video_listener: socket.socket | None = None
        self.live_listener: socket.socket | None = None
        self.control_listener: socket.socket | None = None
        self.sinks: dict[str, VideoSink] = {}
        self.width = 0
        self.height = 0
        self.config_packets: list[bytes] = []
        self.control_lock = threading.Lock()
        self.stop = threading.Event()

    def adb(self, *args: str, timeout: float = 15) -> subprocess.CompletedProcess:
        return subprocess.run(["adb", "-s", self.serial, *args], env=self.env,
                              capture_output=True, text=True, timeout=timeout, check=True)

    @staticmethod
    def _wait_relay_connected(pipe: AdbPipe, timeout: float = 8) -> None:
        assert pipe.process.stderr
        deadline = time.monotonic() + timeout
        output = bytearray()
        while time.monotonic() < deadline:
            ready, _, _ = select.select([pipe.process.stderr], [], [],
                                        deadline - time.monotonic())
            if not ready:
                break
            part = os.read(pipe.process.stderr.fileno(), 1024)
            if not part:
                raise EOFError(f"ADB relay exited: {output[-300:].decode(errors='replace')}")
            output.extend(part)
            if b"PHONE_CAPTURE_RELAY_READY\n" in output:
                return
            if len(output) > 4096:
                raise RuntimeError("ADB relay emitted excessive startup output")
        raise TimeoutError("ADB relay did not connect to scrcpy socket")

    def _listen(self, path: Path) -> socket.socket:
        path.unlink(missing_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        os.chmod(path, 0o600)
        listener.listen(1)
        listener.settimeout(.25)
        return listener

    def _connect_device(self) -> None:
        relay_dir = Path(__file__).resolve().parent / "scrcpy_relay"
        build = relay_dir / "build.sh"
        if (not self.relay_jar.is_file()
                or self.relay_jar.stat().st_mtime < (relay_dir / "AbstractRelay.java").stat().st_mtime):
            subprocess.run(["bash", str(build), str(self.relay_jar)], check=True, timeout=30)
        self.adb("push", str(self.server_jar), self.remote_jar, timeout=30)
        self.remote_jar_pushed = True
        self.adb("push", str(self.relay_jar), self.remote_relay_jar, timeout=30)
        args = ["adb", "-s", self.serial, "shell", f"CLASSPATH={self.remote_jar}",
                "app_process", "/", "com.genymobile.scrcpy.Server", VERSION,
                f"scid={self.scid:08x}", "tunnel_forward=true", "audio=false",
                "control=true", "send_device_meta=false", "send_dummy_byte=false",
                f"max_size={self.max_size}", f"max_fps={self.max_fps}",
                f"video_bit_rate={self.bit_rate}",
                f"video_codec_options=i-frame-interval={self.i_frame_interval}",
                "cleanup=false"]
        self.server_process = subprocess.Popen(args, env=self.env, stdin=subprocess.DEVNULL,
                                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        # The server must bind its abstract socket before a relay connects.
        time.sleep(.75)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.server_process.poll() is not None:
                details = self.server_process.stdout.read().decode(errors="replace") if self.server_process.stdout else ""
                raise RuntimeError(f"scrcpy server exited before relay connected: {details[-500:]}")
            try:
                relay_args = ["adb", "-s", self.serial, "shell", "-T",
                              f"CLASSPATH={self.remote_relay_jar}", "app_process", "/",
                              "org.phonecapture.scrcpy.AbstractRelay",
                              f"scrcpy_{self.scid:08x}"]
                video = AdbPipe(subprocess.Popen(relay_args + ["video"], env=self.env,
                                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                                  stderr=subprocess.PIPE, bufsize=0))
                self._wait_relay_connected(video)
                control = AdbPipe(subprocess.Popen(relay_args + ["control"], env=self.env,
                                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                    stderr=subprocess.PIPE, bufsize=0))
                self._wait_relay_connected(control)
                # The server emits the codec only after both channels connect.
                ready, _, _ = select.select([video], [], [], max(.1, deadline - time.monotonic()))
                if not ready:
                    raise TimeoutError("video relay did not respond")
                codec = exact(video, 4)
                if codec != b"h264":
                    raise RuntimeError(f"unexpected scrcpy codec: {codec!r}")
                if control.process.poll() is not None:
                    raise RuntimeError("control relay exited")
                self.video, self.control = video, control
                return
            except (OSError, EOFError, TimeoutError):
                for conn in (locals().get("video"), locals().get("control")):
                    if conn:
                        conn.close()
                time.sleep(.25)
        raise TimeoutError("scrcpy video/control sockets did not connect")

    def _accept_reader(self, name: str, listener: socket.socket) -> None:
        try:
            reader, _ = listener.accept()
        except socket.timeout:
            return
        previous = self.sinks.pop(name, None)
        if previous:
            previous.close()
        self.sinks[name] = VideoSink(name, reader, self.width, self.height,
                                     self.max_fps, 300 if name == "archive" else 30,
                                     64 * 1024 * 1024 if name == "archive" else 8 * 1024 * 1024)
        # A static screen may never emit another IDR; request a fresh encoder
        # session so each new reader receives SPS/PPS and a decodable frame.
        if self.control:
            with self.control_lock:
                self.control.sendall(bytes([17]))  # TYPE_RESET_VIDEO
        LOG.info("%s video consumer connected for %s", name, self.device_id)

    def _video_loop(self) -> None:
        assert self.video and self.video_listener and self.live_listener
        self.video_listener.settimeout(0)
        self.live_listener.settimeout(0)
        while not self.stop.is_set():
            ready, _, _ = select.select([self.video, self.video_listener,
                                         self.live_listener], [], [], .25)
            if self.video_listener in ready:
                try:
                    self._accept_reader("archive", self.video_listener)
                except BlockingIOError:
                    pass
            if self.live_listener in ready:
                try:
                    self._accept_reader("live", self.live_listener)
                except BlockingIOError:
                    pass
            if self.video not in ready:
                continue
            header = exact(self.video, 12)
            if header[0] & 0x80:  # new capture session, often a rotation
                _, width, height = struct.unpack(">III", header)
                if not (1 <= width <= 65535 and 1 <= height <= 65535):
                    raise ValueError("invalid scrcpy video size")
                changed = self.width and (self.width != width or self.height != height)
                self.width, self.height = width, height
                for sink in self.sinks.values():
                    sink.width, sink.height = width, height
                self.config_packets.clear()
                if changed:
                    for sink in self.sinks.values():
                        sink.close()  # FFmpeg reconnects with new codec parameters
                    self.sinks.clear()
                LOG.info("%s video size %sx%s", self.device_id, width, height)
                continue
            flags, size = struct.unpack(">QI", header)
            if not 0 < size <= MAX_PACKET:
                raise ValueError("invalid scrcpy packet size")
            payload = exact(self.video, size)
            is_config = bool(flags & (1 << 62))
            is_key = bool(flags & (1 << 61))
            pts = flags & ((1 << 61) - 1)
            if is_config:
                self.config_packets.append(payload)
                self.config_packets = self.config_packets[-4:]
                continue
            if not self.sinks or not self.width or not self.height:
                continue
            config = b"".join(self.config_packets) if is_key else b""
            for name, sink in list(self.sinks.items()):
                if not sink.offer(payload, pts, is_key, config):
                    self.sinks.pop(name, None)

    def _send_touch(self, action: int, payload: dict) -> None:
        assert self.control
        self.control.sendall(touch_message(action, payload.get("x"), payload.get("y"),
                                           self.width, self.height))

    def _control_request(self, payload: dict) -> None:
        assert self.control
        kind = payload.get("type")
        with self.control_lock:
            if kind == "key":
                key = payload.get("key")
                if not isinstance(key, str) or key not in KEYCODES:
                    raise ValueError("unsupported key")
                code = KEYCODES[key]
                self.control.sendall(key_message(code, 0) + key_message(code, 1))
            elif kind == "text":
                self.control.sendall(text_message(payload.get("text")))
            elif kind == "tap":
                self._send_touch(0, payload)
                time.sleep(.025)
                self._send_touch(1, payload)
            elif kind == "touch":
                action = {"down": 0, "up": 1, "move": 2}.get(payload.get("action"))
                if action is None:
                    raise ValueError("unsupported touch action")
                self._send_touch(action, payload)
            elif kind == "swipe":
                duration = payload.get("duration_ms")
                if isinstance(duration, bool) or not isinstance(duration, int) or not 50 <= duration <= 5000:
                    raise ValueError("duration_ms must be 50–5000")
                x1, y1 = number(payload.get("x1")), number(payload.get("y1"))
                x2, y2 = number(payload.get("x2")), number(payload.get("y2"))
                self._send_touch(0, {"x": x1, "y": y1})
                steps = max(2, min(60, duration // 16))
                start = time.monotonic()
                for index in range(1, steps):
                    target = start + duration / 1000 * index / steps
                    time.sleep(max(0, target - time.monotonic()))
                    fraction = index / steps
                    self._send_touch(2, {"x": x1 + (x2-x1)*fraction,
                                         "y": y1 + (y2-y1)*fraction})
                time.sleep(max(0, start + duration / 1000 - time.monotonic()))
                self._send_touch(1, {"x": x2, "y": y2})
            else:
                raise ValueError("unsupported control type")

    def _control_client(self, conn: socket.socket) -> None:
        with conn:
            try:
                conn.settimeout(3)
                data = bytearray()
                while b"\n" not in data:
                    part = conn.recv(min(1024, MAX_REQUEST + 1 - len(data)))
                    if not part or len(data) + len(part) > MAX_REQUEST:
                        raise ValueError("invalid control request size")
                    data.extend(part)
                payload = json.loads(data.split(b"\n", 1)[0])
                if not isinstance(payload, dict):
                    raise ValueError("control request must be an object")
                self._control_request(payload)
                conn.sendall(b'{"ok":true}\n')
            except (ValueError, json.JSONDecodeError) as exc:
                conn.sendall(json.dumps({"error": str(exc)}).encode() + b"\n")
            except OSError:
                pass

    def _control_loop(self) -> None:
        assert self.control_listener
        while not self.stop.is_set():
            try:
                conn, _ = self.control_listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self.stop.is_set():
                    return
                raise
            threading.Thread(target=self._control_client, args=(conn,), daemon=True).start()

    def run(self) -> None:
        try:
            self.runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.runtime_dir, 0o700)
            self.lock_file = (self.runtime_dir / f"{self.device_id}.lock").open("a+b")
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.lock_acquired = True
            self._connect_device()
            self.video_listener = self._listen(self.video_socket_path)
            self.live_listener = self._listen(self.live_socket_path)
            self.control_listener = self._listen(self.control_socket_path)
            threading.Thread(target=self._control_loop, daemon=True).start()
            print(f"READY {self.video_socket_path} {self.live_socket_path} {self.control_socket_path}", flush=True)
            try:
                self._video_loop()
            except EOFError:
                if not self.stop.is_set():
                    raise
        finally:
            self.close()

    def close(self) -> None:
        self.stop.set()
        if not self.lock_acquired:
            if self.lock_file:
                self.lock_file.close()
            return
        for sink in self.sinks.values():
            sink.close()
        self.sinks.clear()
        for s in (self.video, self.control, self.video_listener,
                  self.live_listener, self.control_listener):
            if s:
                s.close()
        for path in (self.video_socket_path, self.live_socket_path,
                     self.control_socket_path):
            path.unlink(missing_ok=True)
        if self.server_process:
            self.server_process.terminate()
            try:
                self.server_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.server_process.kill()
        try:
            if self.remote_jar_pushed:
                self.adb("shell", "rm", self.remote_jar, self.remote_relay_jar, timeout=5)
        except (OSError, subprocess.SubprocessError):
            LOG.warning("Could not clean scrcpy ADB tunnel for %s", self.device_id)
        if self.lock_file:
            self.lock_file.close()
            self.lock_file = None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device-id", required=True, choices=("pixel-4", "pixel-4-xl"))
    parser.add_argument("--serial", required=True)
    parser.add_argument("--adb-server-socket")
    parser.add_argument("--tunnel-host", default="127.0.0.1")
    parser.add_argument("--server-jar", type=Path, default=Path(__file__).resolve().parent / ".tools/scrcpy-linux-x86_64-v4.1/scrcpy-server")
    parser.add_argument("--relay-jar", type=Path, default=Path(__file__).resolve().parent / ".tools/scrcpy-abstract-relay.jar")
    parser.add_argument("--runtime-dir", type=Path, default=Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "phone-capture")
    parser.add_argument("--bit-rate", type=int, default=4_000_000)
    parser.add_argument("--max-size", type=int, default=720)
    parser.add_argument("--max-fps", type=int, default=30)
    parser.add_argument("--i-frame-interval", type=int, default=1,
                        help="scrcpy encoder keyframe interval in seconds (1–10)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    bridge = ScrcpyBridge(device_id=args.device_id, serial=args.serial,
                          adb_server_socket=args.adb_server_socket,
                          tunnel_host=args.tunnel_host, server_jar=args.server_jar,
                          relay_jar=args.relay_jar,
                          runtime_dir=args.runtime_dir, bit_rate=args.bit_rate,
                          max_size=args.max_size, max_fps=args.max_fps,
                          i_frame_interval=args.i_frame_interval)
    def shutdown(_signal, _frame):
        bridge.stop.set()
    signal.signal(signal.SIGTERM, shutdown)
    try:
        bridge.run()
    except KeyboardInterrupt:
        bridge.close()


if __name__ == "__main__":
    main()
