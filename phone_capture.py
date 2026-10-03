#!/usr/bin/env python3
"""ADB screen capture for Pixel 4 / 4 XL, with local retention and viewer upload.

scrcpy runs continuously without a playback window. FFmpeg closes short MP4
segments, which are uploaded to the viewer and mirrored onto the phone.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

LOG = logging.getLogger("phone-capture")
PHONE_ROOT = "/sdcard/Movies/PhoneCapture"
VALID_SERIAL = re.compile(r"^[A-Za-z0-9._:-]+$")
VALID_SESSION = re.compile(r"^[A-Za-z0-9_-]+$")
VALID_FILE = re.compile(r"^(?:\d{10}|\d{8}T\d{6}Z-(?:\d{10}|[0-9a-f]{6}))\.mp4$")


def is_archive_path(path: str) -> bool:
    prefix = PHONE_ROOT + "/"
    if not path.startswith(prefix):
        return False
    parts = path[len(prefix):].split("/")
    return (len(parts) == 2 and bool(VALID_SESSION.fullmatch(parts[0]))
            and bool(VALID_FILE.fullmatch(parts[1])))


def thermal_state(health: int | None, temperature_c: float | None,
                  was_paused: bool) -> bool:
    """Follow Android health; use 50/48°C as a hard fallback and hysteresis."""
    if health == 3:  # BatteryManager.BATTERY_HEALTH_OVERHEAT
        return True
    if health in (4, 5, 6, 7):  # Other unhealthy battery states.
        return True
    if temperature_c is not None and temperature_c >= 50:
        return True
    if was_paused:
        if health == 2:  # BatteryManager.BATTERY_HEALTH_GOOD
            return temperature_c is not None and temperature_c > 48
        return temperature_c is None or temperature_c > 48
    if health not in (1, 2, None):
        return True
    if health in (1, None) and temperature_c is None:
        return True
    return False


def adb(serial: str | None, *args: str, socket: str | None = None,
        timeout: int = 30) -> subprocess.CompletedProcess[str]:
    command = ["adb"]
    if serial:
        command += ["-s", serial]
    env = dict(os.environ)
    if socket:
        env["ADB_SERVER_SOCKET"] = socket
    else:
        env.pop("ADB_SERVER_SOCKET", None)
    return subprocess.run(command + list(args), capture_output=True, text=True,
                          timeout=timeout, env=env)


def devices(socket: str | None) -> set[str]:
    result = adb(None, "devices", socket=socket, timeout=10)
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return {line.split()[0] for line in result.stdout.splitlines()[1:]
            if len(line.split()) >= 2 and line.split()[1] == "device"
            and VALID_SERIAL.fullmatch(line.split()[0])}


class PhoneWorker:
    def __init__(self, serial: str, socket: str | None, config: argparse.Namespace):
        self.serial, self.socket, self.config = serial, socket, config
        self.stop = threading.Event()
        self.upload_wakeup = threading.Event()
        self.root = Path(config.state_dir) / serial
        self.root.mkdir(parents=True, exist_ok=True)
        self.token = os.environ.get("RECORDINGS_PHONE_TOKEN") or Path(config.token_file).read_text().strip()
        self.thread = threading.Thread(target=self.run, name=f"capture-{serial}", daemon=True)
        self.uploader = threading.Thread(target=self.upload_loop, name=f"upload-{serial}", daemon=True)
        self.device_id: str | None = None
        self.session_id: str | None = None
        self.capture_process: subprocess.Popen | None = None
        self.mux_process: subprocess.Popen | None = None
        self.retry_after: dict[str, float] = {}
        self.session_started = 0.0
        self.paused_hot = False

    def adb(self, *args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
        return adb(self.serial, *args, socket=self.socket, timeout=timeout)

    def request(self, method: str, path: str, data: bytes | None = None,
                content_type: str = "application/json") -> dict:
        url = self.config.server.rstrip("/") + path
        headers = {"X-Phone-Token": self.token}
        if data is not None:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read()
        return json.loads(body) if body else {}

    def identify(self) -> bool:
        model = self.adb("shell", "getprop", "ro.product.model", timeout=10)
        if model.returncode:
            return False
        name = model.stdout.strip().lower()
        self.device_id = {"pixel 4": "pixel-4", "pixel 4 xl": "pixel-4-xl"}.get(name)
        if self.device_id is None:
            LOG.info("Ignoring %s: unsupported model %r", self.serial, name)
            return False
        LOG.info("Found %s (%s)", self.device_id, self.serial)
        return True

    def battery(self) -> dict:
        result = self.adb("shell", "dumpsys", "battery", timeout=10)
        values: dict[str, int | float | None] = {}
        if result.returncode == 0:
            for field in ("level", "temperature", "health"):
                match = re.search(rf"^\s*{field}:\s*(\d+)", result.stdout, re.MULTILINE)
                if match:
                    values[field] = int(match.group(1))
        return {"battery_percent": values.get("level"),
                "temperature_c": values["temperature"] / 10 if "temperature" in values else None,
                "battery_health": values.get("health")}

    def check_thermal(self) -> bool:
        reading = self.battery()
        now_paused = thermal_state(reading["battery_health"], reading["temperature_c"], self.paused_hot)
        if now_paused != self.paused_hot:
            LOG.warning("%s thermal capture state: %s (battery %s)", self.device_id,
                        "paused_hot" if now_paused else "live", reading)
        self.paused_hot = now_paused
        reading["capture_state"] = "paused_hot" if now_paused else "live"
        try:
            self.request("POST", f"/api/phones/{self.device_id}/status", json.dumps(reading).encode())
        except (OSError, ValueError, urllib.error.URLError) as exc:
            LOG.debug("Could not report thermal status: %s", exc)
        return now_paused

    def start_session(self) -> bool:
        name = f"{self.device_id} screen"
        try:
            sid = uuid.uuid4().hex
            reply = self.request("POST", f"/api/phones/{self.device_id}/sessions",
                                 json.dumps({"name": name, "session_id": sid}).encode())
            sid = str(reply["session_id"])
            if not VALID_SESSION.fullmatch(sid):
                raise ValueError(f"unsafe session ID: {sid!r}")
            self.session_id = sid
            self.session_started = time.monotonic()
            LOG.info("Session %s started on %s", sid, self.device_id)
            return True
        except (OSError, ValueError, KeyError, urllib.error.URLError) as exc:
            LOG.warning("Cannot start viewer session for %s: %s", self.serial, exc)
            return False

    def stop_session(self, sid: str) -> None:
        pending = self.root / sid / "stop.pending"
        pending.parent.mkdir(parents=True, exist_ok=True)
        pending.touch()
        try:
            self.request("POST", f"/api/phones/{self.device_id}/sessions/{sid}/stop", b"{}")
            pending.unlink(missing_ok=True)
        except Exception as exc:
            LOG.warning("Will retry stop for viewer session %s: %s", sid, exc)

    def run(self) -> None:
        if not self.identify():
            return
        self.uploader.start()
        while not self.stop.is_set():
            if self.check_thermal():
                if self.session_id:
                    self.stop_session(self.session_id)
                    self.session_id = None
                self.stop.wait(10)
                continue
            if not self.session_id and not self.start_session():
                self.stop.wait(5)
                continue
            sid = self.session_id
            if self.socket:
                self.run_screenrecord(sid)
                continue
            local_dir = self.root / sid
            local_dir.mkdir(parents=True, exist_ok=True)
            fifo = local_dir / "scrcpy.mkv"
            fifo.unlink(missing_ok=True)
            os.mkfifo(fifo, 0o600)
            output = str(local_dir / "%010d.mp4")
            ffmpeg_command = [self.config.ffmpeg, "-hide_banner", "-loglevel", "warning",
                              "-i", str(fifo), "-map", "0:v:0", "-an",
                              "-vf", "fps=10", "-c:v", "libx264", "-preset", "ultrafast",
                              "-crf", "24", "-g", "50", "-keyint_min", "50",
                              "-sc_threshold", "0", "-f", "segment", "-segment_time",
                              str(self.config.segment_seconds), "-segment_start_number",
                              str(int(time.time())), "-reset_timestamps", "1", output]
            scrcpy_command = [self.config.scrcpy, "--serial", self.serial,
                              "--no-playback", "--no-control", "--no-audio",
                              "--max-size", str(self.config.max_size),
                              "--video-bit-rate", self.config.bit_rate,
                              "--record-format=mkv", "--record", str(fifo)]
            env = dict(os.environ)
            env["SDL_VIDEODRIVER"] = "dummy"
            if self.socket:
                env["ADB_SERVER_SOCKET"] = self.socket
            else:
                env.pop("ADB_SERVER_SOCKET", None)
            try:
                with (local_dir / "ffmpeg.log").open("ab") as ff_log, (local_dir / "scrcpy.log").open("ab") as sc_log:
                    self.mux_process = subprocess.Popen(ffmpeg_command, stdout=ff_log, stderr=ff_log)
                    self.capture_process = subprocess.Popen(scrcpy_command, stdout=sc_log,
                                                            stderr=sc_log, env=env)
                    LOG.info("Capturing %s continuously", self.device_id)
                    last_thermal_check = time.monotonic()
                    while not self.stop.wait(1):
                        self.upload_wakeup.set()
                        if time.monotonic() - last_thermal_check >= 5:
                            last_thermal_check = time.monotonic()
                            if self.check_thermal():
                                LOG.warning("Stopping %s capture while battery is hot", self.device_id)
                                break
                        if time.monotonic() - self.session_started >= self.config.session_seconds:
                            LOG.info("Rotating hour-long %s session", self.device_id)
                            break
                        if self.capture_process.poll() is not None or self.mux_process.poll() is not None:
                            LOG.warning("Capture process stopped for %s; restarting", self.device_id)
                            break
            except OSError as exc:
                LOG.error("Cannot start capture for %s: %s", self.device_id, exc)
            finally:
                # Close scrcpy's FIFO writer first so FFmpeg can flush its
                # final MP4 and exit normally before the uploader sees it.
                for proc in (self.capture_process, self.mux_process):
                    if proc and proc.poll() is None:
                        if proc is self.capture_process:
                            proc.terminate()
                        try:
                            proc.wait(timeout=8)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                self.capture_process = self.mux_process = None
                fifo.unlink(missing_ok=True)
                self.upload_wakeup.set()
            if self.session_id:
                self.stop_session(sid)
                self.session_id = None
            self.stop.wait(3)
        if self.session_id:
            self.stop_session(self.session_id)

    def run_screenrecord(self, sid: str) -> None:
        """Fallback for ADB servers on another host (scrcpy tunnel is local)."""
        remote_dir = f"{PHONE_ROOT}/{sid}"
        mkdir = self.adb("shell", "mkdir", "-p", remote_dir, timeout=10)
        if mkdir.returncode:
            LOG.warning("Cannot make phone archive: %s", mkdir.stderr.strip())
            self.stop.wait(3)
            return
        local_dir = self.root / sid
        local_dir.mkdir(parents=True, exist_ok=True)
        while not self.stop.is_set():
            if self.check_thermal():
                self.stop_session(sid)
                self.session_id = None
                return
            if time.monotonic() - self.session_started >= self.config.session_seconds:
                self.stop_session(sid)
                self.session_id = None
                return
            filename = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{uuid.uuid4().hex[:6]}.mp4"
            remote = f"{remote_dir}/{filename}"
            try:
                result = self.adb("shell", "screenrecord", "--time-limit",
                                  str(self.config.segment_seconds), "--bit-rate",
                                  self.config.bit_rate, "--size", "720x1520",
                                  remote, timeout=self.config.segment_seconds + 20)
                if result.returncode:
                    raise OSError(result.stderr.strip())
                pulled = self.adb("pull", remote, str(local_dir / filename), timeout=90)
                if pulled.returncode:
                    raise OSError(pulled.stderr.strip())
                (local_dir / filename).with_suffix(".mirrored").write_text(remote + "\n")
                self.upload_wakeup.set()
            except (OSError, subprocess.TimeoutExpired) as exc:
                LOG.warning("screenrecord fallback failed on %s: %s", self.device_id, exc)
                self.stop.wait(3)

    def upload_one(self, sid: str, local: Path) -> bool:
        filename = local.name
        if time.monotonic() < self.retry_after.get(str(local), 0):
            return False
        try:
            mirrored = local.with_suffix(".mirrored")
            remote_dir = f"{PHONE_ROOT}/{sid}"
            if mirrored.exists() and mirrored.read_text().strip():
                remote_path = mirrored.read_text().strip()
            elif self.socket:
                remote_path = f"{remote_dir}/{filename}"
            else:
                remote_dir = f"{PHONE_ROOT}/{sid}"
                mkdir = self.adb("shell", "mkdir", "-p", remote_dir, timeout=10)
                if mkdir.returncode:
                    raise OSError(mkdir.stderr.strip())
                remote_path = f"{remote_dir}/{filename}"
                pushed = self.adb("push", str(local), remote_path, timeout=90)
                if pushed.returncode:
                    raise OSError(pushed.stderr.strip())
                mirrored.write_text(remote_path + "\n")
            captured_at = datetime.fromtimestamp(max(0, local.stat().st_mtime - self.config.segment_seconds), timezone.utc).isoformat()
            query = urllib.parse.urlencode({"session_id": sid, "segment_key": filename[:-4],
                                            "captured_at": captured_at,
                                            "remote_path": remote_path})
            self.request("POST", f"/api/phones/{self.device_id}/segments?{query}",
                         local.read_bytes(), "video/mp4")
            local.unlink(missing_ok=True)
            mirrored.unlink(missing_ok=True)
            self.retry_after.pop(str(local), None)
            LOG.info("Uploaded and archived %s %s", self.device_id, filename)
            return True
        except urllib.error.HTTPError as exc:
            if exc.code == 422:
                rejected = local.with_suffix(".rejected")
                local.rename(rejected)
                LOG.error("Quarantined invalid clip %s", rejected)
                return True
            self.retry_after[str(local)] = time.monotonic() + 10
            LOG.warning("Upload pending %s: %s", filename, exc)
            return False
        except (OSError, ValueError, urllib.error.URLError) as exc:
            self.retry_after[str(local)] = time.monotonic() + 10
            LOG.warning("Upload pending %s: %s", filename, exc)
            return False

    def prune_phone(self) -> None:
        capacity = self.adb("shell", "df", "-k", "/sdcard", timeout=10)
        if capacity.returncode:
            return
        lines = capacity.stdout.splitlines()
        if len(lines) < 2:
            return
        fields = lines[-1].split()
        if len(fields) < 4 or not fields[1].isdigit() or not fields[3].isdigit():
            return
        capacity_total_kib, free_kib = int(fields[1]), int(fields[3])
        battery_fields = self.battery()
        try:
            settings = self.request("GET", f"/api/phones/{self.device_id}/settings")
            mode = settings.get("retention_mode", "percent")
            value = float(settings.get("retention_value", 50))
        except (OSError, ValueError, urllib.error.URLError) as exc:
            LOG.warning("Retention settings unavailable: %s", exc)
            mode, value = "percent", 50.0
        if mode == "percent":
            limit = int(capacity_total_kib * min(max(value, 0), 100) / 100)
        elif mode == "gib":
            limit = int(max(value, 0) * 1024 * 1024)
        else:
            LOG.warning("Unknown retention mode %r; using 50%%", mode)
            limit = capacity_total_kib // 2
        # Reserve 5 GiB for the phone's own apps and new recordings.
        free_headroom_kib = 5 * 1024 * 1024
        result = self.adb("shell", "du", "-ak", PHONE_ROOT, timeout=60)
        if result.returncode:
            return
        files = []
        total_kib = 0
        for line in result.stdout.splitlines():
            fields = line.split(maxsplit=1)
            if len(fields) != 2 or not fields[0].isdigit():
                continue
            path = fields[1]
            if not is_archive_path(path):
                continue
            size = int(fields[0])
            total_kib += size
            files.append((Path(path).name, size, path))
        try:
            self.request("POST", f"/api/phones/{self.device_id}/status",
                         json.dumps({"device_storage_total_bytes": capacity_total_kib * 1024,
                                     "device_storage_free_bytes": free_kib * 1024,
                                     "device_archive_bytes": total_kib * 1024,
                                     "adb_serial": self.serial,
                                     "adb_server_socket": self.socket,
                                     "battery_percent": battery_fields.get("battery_percent"),
                                     "temperature_c": battery_fields.get("temperature_c"),
                                     "battery_health": battery_fields.get("battery_health"),
                                     "capture_state": "paused_hot" if self.paused_hot else "live"}).encode())
        except (OSError, ValueError, urllib.error.URLError) as exc:
            LOG.debug("Could not report phone storage: %s", exc)
        removed_keys: dict[str, list[str]] = {}
        for _, size, path in sorted(files):
            if total_kib <= limit and free_kib >= free_headroom_kib:
                break
            # Bound every remote path to our private generated tree.
            removed = self.adb("shell", "rm", path, timeout=10)
            if removed.returncode == 0:
                total_kib -= size
                free_kib += size
                relative = path[len(PHONE_ROOT) + 1:].split("/")
                sid, filename = relative
                key = filename.removesuffix(".mp4")
                if re.fullmatch(r"\d{8}T\d{6}Z-\d{10}", key):
                    key = key.rsplit("-", 1)[1]
                removed_keys.setdefault(sid, []).append(key)
                LOG.info("Pruned old phone segment %s", path)
        for sid, keys in removed_keys.items():
            event = self.root / f"prune-{uuid.uuid4().hex}.json"
            event.write_text(json.dumps({"session_id": sid, "segment_keys": keys}))
        self.flush_prune_events()

    def flush_prune_events(self) -> None:
        for event in sorted(self.root.glob("prune-*.json")):
            try:
                self.request("POST", f"/api/phones/{self.device_id}/archive-prune", event.read_bytes())
                event.unlink(missing_ok=True)
            except (OSError, ValueError, urllib.error.URLError) as exc:
                LOG.warning("Archive prune notification pending: %s", exc)
                break

    def upload_loop(self) -> None:
        last_prune = 0.0
        while not self.stop.is_set():
            self.upload_wakeup.wait(2)
            self.upload_wakeup.clear()
            self.upload_pending()
            self.flush_prune_events()
            if time.monotonic() - last_prune > 60:
                self.prune_phone()
                self.prune_host()
                last_prune = time.monotonic()

    def prune_host(self) -> None:
        """Bound unsent clips while the viewer is unavailable."""
        clips = list(self.root.glob("*/*.mp4"))
        sizes = [(clip.stat().st_mtime, clip.stat().st_size, clip) for clip in clips]
        total = sum(size for _, size, _ in sizes)
        limit = int(self.config.max_host_gib * 1024 ** 3)
        active_dir = self.root / self.session_id if self.session_id else None
        active_open = max(active_dir.glob("*.mp4"), default=None) if active_dir and active_dir.exists() else None
        for _, size, clip in sorted(sizes):
            if total <= limit:
                break
            if clip == active_open:
                continue
            clip.unlink(missing_ok=True)
            self.retry_after.pop(str(clip), None)
            total -= size
            LOG.warning("Host spool limit reached; dropped oldest pending clip %s", clip)

    def upload_pending(self) -> None:
        for directory in sorted(self.root.iterdir()):
            if not directory.is_dir() or not VALID_SESSION.fullmatch(directory.name):
                continue
            # Markers survive VM spool eviction. Recover one missing clip per
            # scan from the authoritative phone archive when the viewer is up.
            for marker in sorted(directory.glob("*.mirrored")):
                clip = marker.with_suffix(".mp4")
                if clip.exists():
                    continue
                try:
                    self.request("GET", f"/api/phones/{self.device_id}/settings")
                    pulled = self.adb("pull", marker.read_text().strip(), str(clip), timeout=90)
                    if pulled.returncode:
                        raise OSError(pulled.stderr.strip())
                    LOG.info("Recovered pending clip from phone %s", clip.name)
                except (OSError, ValueError, urllib.error.URLError) as exc:
                    LOG.debug("Phone backlog still pending: %s", exc)
                break
            clips = sorted(directory.glob("*.mp4"))
            # FFmpeg is still writing its newest file until the next segment opens.
            if directory.name == self.session_id and self.mux_process and self.mux_process.poll() is None:
                clips = clips[:-1]
            for clip in clips:
                if not self.upload_one(directory.name, clip):
                    break
            pending = directory / "stop.pending"
            if pending.exists():
                self.stop_session(directory.name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="http://127.0.0.1:8765")
    parser.add_argument("--token-file", default=os.environ.get("PHONE_CAPTURE_TOKEN_FILE"))
    parser.add_argument("--state-dir", default=os.environ.get("PHONE_CAPTURE_STATE_DIR") or
                        str(Path(__file__).resolve().parent / "state"))
    parser.add_argument("--segment-seconds", type=int, default=5)
    parser.add_argument("--bit-rate", default="4M")
    parser.add_argument("--max-size", type=int, default=720)
    parser.add_argument("--scrcpy", default=shutil.which("scrcpy") or str(Path(__file__).resolve().parent / ".tools/scrcpy-linux-x86_64-v4.1/scrcpy"))
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg") or "ffmpeg")
    parser.add_argument("--adb-server-socket", action="append",
                        default=[s for s in os.environ.get("PHONE_CAPTURE_ADB_SERVER_SOCKETS", "").split(",") if s],
                        help="additional ADB server socket (e.g. tcp:192.0.2.10:5037)")
    parser.add_argument("--poll-seconds", type=float, default=3)
    parser.add_argument("--max-host-gib", type=float, default=2)
    parser.add_argument("--session-seconds", type=int, default=3600)
    parser.add_argument("--verbose", action="store_true")
    config = parser.parse_args()
    if not (os.environ.get("RECORDINGS_PHONE_TOKEN") or config.token_file):
        parser.error("set RECORDINGS_PHONE_TOKEN or --token-file / PHONE_CAPTURE_TOKEN_FILE")
    if not 1 <= config.segment_seconds <= 180:
        parser.error("segment-seconds must be between 1 and 180")
    logging.basicConfig(level=logging.DEBUG if config.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    workers: dict[tuple[str | None, str], PhoneWorker] = {}
    def stop_on_signal(signum: int, _frame: object) -> None:
        LOG.info("Received signal %s", signum)
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop_on_signal)
    try:
        while True:
            try:
                connected = {(socket, serial) for socket in [None, *config.adb_server_socket]
                             for serial in devices(socket)}
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                LOG.warning("ADB scan failed: %s", exc)
                connected = set()
            for key, worker in list(workers.items()):
                if key not in connected:
                    worker.stop.set()
                if not worker.thread.is_alive() and not worker.uploader.is_alive():
                    worker.thread.join(timeout=0)
                    del workers[key]
            for socket, serial in connected - workers.keys():
                worker = PhoneWorker(serial, socket, config)
                workers[(socket, serial)] = worker
                worker.thread.start()
            time.sleep(config.poll_seconds)
    except KeyboardInterrupt:
        LOG.info("Stopping")
    finally:
        for worker in workers.values():
            worker.stop.set()
        for worker in workers.values():
            worker.thread.join(timeout=config.segment_seconds + 20)
            worker.uploader.join(timeout=10)
            worker.upload_pending()


if __name__ == "__main__":
    main()
