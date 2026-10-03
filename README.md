# Phone Capture

Screen capture for plugged-in Pixel 4 and Pixel 4 XL phones. A host daemon watches ADB. Phones connected through the local ADB server use [scrcpy](https://github.com/Genymobile/scrcpy) in headless recording mode and FFmpeg to make short MP4 clips from one continuous stream. Phones reached through an ADB server on another host use Android `screenrecord` because scrcpy's local video tunnel cannot cross that setup. The daemon archives each completed clip in `/sdcard/Movies/PhoneCapture/<session-id>/` on the phone and uploads it to a recordings viewer for live playback and history.

This captures the **screen**, including tests and app UI. It does not activate the phone camera or microphone. Those can be added as separate tracks later.

## Requirements

- Python 3.10+, `adb`, `ffmpeg`, and a running recordings viewer on `127.0.0.1:8765`.
- USB debugging authorized on each phone. If a phone uses another ADB server, set `PHONE_CAPTURE_ADB_SERVER_SOCKETS` to its socket address.
- The viewer's phone token file must be readable by the user running this daemon. Point `PHONE_CAPTURE_TOKEN_FILE` to it, or set `RECORDINGS_PHONE_TOKEN` in the service environment.
- `scrcpy` 4.1. Run `./install_scrcpy.sh`; it checks the release tarball SHA-256 before extraction. Its binary is kept under `.tools/` and excluded from git.

## Run

```bash
./install_scrcpy.sh
python3 phone_capture.py --token-file /path/to/recordings/phone_token
```

The daemon scans the default ADB server and configured additional sockets every three seconds, identifies the phones by Android model, and assigns stable viewer IDs `pixel-4` and `pixel-4-xl`. Add a remote socket with `--adb-server-socket tcp:192.0.2.10:5037` or the `PHONE_CAPTURE_ADB_SERVER_SOCKETS` environment variable (comma-separated for multiple sockets). It starts a new session after each connection or scrcpy restart. Both phones can record concurrently. It uses `SDL_VIDEODRIVER=dummy` so no desktop session is required.

Install the example `phone-capture.service` in `~/.config/systemd/user/`. It assumes this repository is checked out at `~/phone-capture` and requires a `recordings-viewer.service` user unit; edit the paths and unit dependency for your setup. Create `~/.config/phone-capture/env` with `PHONE_CAPTURE_TOKEN_FILE=/path/to/recordings/phone_token`. Add `PHONE_CAPTURE_ADB_SERVER_SOCKETS=tcp:192.0.2.10:5037` if needed, and `PHONE_CAPTURE_STATE_DIR=/path/on/a/data/drive/phone-capture` if the default spool location is too small. Keep the environment file private (`chmod 600`). Then run `systemctl --user enable --now phone-capture.service`. Use `journalctl --user -u phone-capture.service -f` for logs.

## Retention and latency

The viewer's per-device settings choose a size in GiB or a percent of the phone's total storage; default is **50%**. The daemon reads `df -k /sdcard` and prunes the oldest archived MP4s when the archive exceeds the limit or free phone storage falls below 5 GiB. It notifies the viewer of pruned clips so history matches what remains on the phone. Uploads are idempotent. Host state lives under `state/` inside the checkout by default, with a **2 GiB per-phone pending-clip limit**. The phone is archived first. If the viewer is unavailable long enough for the host spool to fill, a durable marker lets the daemon fetch the clip back from the phone when the viewer returns.

Scrcpy capture is continuous, so there is no planned gap between its clips. `screenrecord` restarts after each clip and pull, leaving a brief gap. Closed clips reach the viewer about one segment duration plus transfer time after capture; the default segment target is five seconds. On a static screen, Android's encoder may emit few frames, which delays scrcpy clip closure until new frames arrive. The live player holds its last image during that pause.

Logical sessions rotate hourly so history playlists stay small. The Pixel 4 scrcpy pipeline restarts briefly at rotation. Status reports the phone's battery level, **battery temperature** (not ambient room temperature), storage capacity/free space, and ADB route.

Capture pauses if Android reports battery health `OVERHEAT` (3), another unhealthy battery state, or the battery reaches the 50°C hard limit. Android reports `GOOD` as 2; a `GOOD` phone near 45°C keeps capturing. After a pause, it resumes when health is `GOOD` and temperature is at most 48°C. If health is unavailable, temperature controls the same 50/48°C fallback; if both readings are unavailable, capture pauses. The daemon keeps reporting battery status and checks for recovery every ten seconds. The phone's last recordings remain available in history during a pause. [Android BatteryManager constants](https://developer.android.com/reference/android/os/BatteryManager#BATTERY_HEALTH_OVERHEAT)

The viewer API used is `POST /api/phones/{id}/sessions`, `POST /api/phones/{id}/segments?session_id=…&segment_key=…&captured_at=…&remote_path=…`, `POST /api/phones/{id}/sessions/{session_id}/stop`, `GET /api/phones/{id}/settings`, `POST /api/phones/{id}/status`, and `POST /api/phones/{id}/archive-prune`. Write calls send `X-Phone-Token`. The viewer can re-fetch older clips from `remote_path` through the reported ADB route after its live TS cache rotates out.
