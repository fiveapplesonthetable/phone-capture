# Phone Capture

Screen capture for plugged-in Pixel 4 and Pixel 4 XL phones. A host daemon watches ADB. In bridge mode, one [scrcpy](https://github.com/Genymobile/scrcpy) server per phone supplies timestamped H.264 video and an interactive control channel over ADB. FFmpeg sends that video to a local RTSP/WebRTC gateway and closes short MP4 clips for the phone archive and [recordings](https://github.com/fiveapplesonthetable/recordings) viewer history. The bridge works through a remote ADB server without opening a TCP port on the phone or ADB host.

This captures the **screen**, including tests and app UI. It does not activate the phone camera or microphone. Those can be added as separate tracks later.

## Requirements

- Python 3.10+, `adb`, `ffmpeg`, [PyAV](https://pyav.org/), and a running recordings viewer.
- USB debugging authorized on each phone. Additional ADB servers can be supplied with `--adb-server-socket tcp:<host>:5037`.
- A viewer phone token readable through `--token-file`, or `RECORDINGS_PHONE_TOKEN` in the service environment.
- `scrcpy` 4.1. Run `./install_scrcpy.sh`; it checks the release tarball SHA-256 before extraction. Its binary is kept under `.tools/` and excluded from git.
- Java 8+ and Android SDK platform/build tools for the small Android abstract-socket relay. `scrcpy_relay/build.sh` builds it on the first bridge start; set `ANDROID_HOME` if the SDK is outside the usual location.
- A local RTSP/WebRTC gateway such as MediaMTX for browser playback. Set `--scrcpy-rtsp-base` to its loopback RTSP address without a device path.

## Run

```bash
./install_scrcpy.sh
python3 phone_capture.py --token-file /path/to/viewer/phone_token \
  --adb-server-socket tcp:<adb-host>:5037 \
  --scrcpy-bridge-device pixel-4 --scrcpy-bridge-device pixel-4-xl \
  --scrcpy-rtsp-base rtsp://127.0.0.1:8554 --scrcpy-live-fanout --max-size 1520
```

The daemon scans the default ADB server and any additional sockets every three seconds, identifies the phones by Android model, and assigns stable viewer IDs `pixel-4` and `pixel-4-xl`. Both phones can run the same bridge path. Each bridge exposes mode-0600 Unix video and control sockets under `$XDG_RUNTIME_DIR/phone-capture/`; the viewer can send touch and key events through its control socket. With `--scrcpy-live-fanout`, one FFmpeg process copies H.264 to RTSP while another independently encodes archived MP4 clips. If RTSP fails, recording continues and the live publisher retries. The service needs no desktop session.

A VM-local Cuttlefish emulator can use the same bridge. Add `--scrcpy-bridge-device cuttlefish` when it appears in plain `adb devices`; the viewer ID is `cuttlefish`. Its rolling archive lives under the emulator's `/sdcard/Movies/PhoneCapture/` and obeys the same size setting and free-space reserve.

### Optional direct live side path

The bridge can expose the same encoded H.264 packets on a bounded `<device-id>.raw.sock` Unix socket. `direct_rtsp_publisher.py` sends them to a second local MediaMTX gateway with PyAV, without decoding or re-encoding. The existing `.live.sock` publisher and phone archive continue independently. Enable this per device with `--scrcpy-direct-device <device-id>` and point `--scrcpy-direct-rtsp-base` at the second gateway. A raw reader requests a fresh keyframe on connection instead of replaying a cached group of pictures.

`direct-mediamtx.yml` is a second-gateway example on RTSP port 18555 and WHEP port 18890. Run it with `mediamtx direct-mediamtx.yml` and supervise it with your service manager. Replace the placeholders in `phone-capture-direct-gateway.service` for a systemd user service.

```bash
python3 phone_capture.py --token-file /path/to/viewer/phone_token \
  --scrcpy-bridge-device pixel-4-xl --scrcpy-live-fanout \
  --scrcpy-rtsp-base rtsp://127.0.0.1:8554 \
  --scrcpy-direct-device pixel-4-xl \
  --scrcpy-direct-rtsp-base rtsp://127.0.0.1:18555
```

The side path publishes `pixel-4-xl-direct` (or the corresponding opted-in device ID). If its gateway disappears, only the side publisher restarts; the original RTSP stream and MP4 recording continue. The daemon also checks for a publisher stuck on a gateway that restarted while the screen was static. A viewer can prefer the side WHEP path and fall back to the original. Keep the normal thermal guard active on physical phones.

For viewers that request a fresh picture after an unchanged screen, `--scrcpy-refresh-device cuttlefish` opts that device into a one-shot `{"type":"refresh_video"}` control request. The bridge only acts when the live stream has been idle for at least two seconds and limits requests to one per 20 seconds. Other devices remain unchanged unless explicitly opted in.

Set `PHONE_CAPTURE_TOKEN_FILE` and any `PHONE_CAPTURE_ADB_SERVER_SOCKETS`/`PHONE_CAPTURE_STATE_DIR` values in `~/.config/phone-capture/env`. Edit the loopback RTSP address in the example `phone-capture.service`, then install it in `~/.config/systemd/user/` and run `systemctl --user enable --now phone-capture.service`. It starts after the viewer and retries uploads if the viewer restarts. Use `journalctl --user -u phone-capture.service -f` for logs.

## Retention and latency

The viewer's per-device settings choose a size in GiB or a percent of the phone's total storage; default is **50%**. The daemon reads `df -k /sdcard` and prunes the oldest archived MP4s when the archive exceeds the limit or free phone storage falls below 5 GiB. It notifies the viewer of pruned clips so history matches what remains on the phone. Uploads are idempotent. Host state defaults to `state/` beside the daemon, with a **2 GiB per-phone pending-clip limit**. The phone is archived first. If the viewer is unavailable long enough for the VM spool to fill, a durable marker lets the daemon fetch the clip back from the phone when the viewer returns.

Scrcpy bridge capture is continuous on either phone, so there is no planned gap between its clips. Closed clips reach the viewer about one segment duration plus transfer time after capture; the default segment target is five seconds. The live RTSP/WebRTC feed is available before a clip closes. If the bridge fails repeatedly, capture returns to the existing `screenrecord` fallback. On a static screen, Android's encoder may emit few frames, which delays clip closure until new frames arrive; live playback holds the last image.

Logical sessions rotate hourly so history playlists stay small. The scrcpy pipeline restarts briefly at rotation. Status reports the phone's battery level, **battery temperature** (not ambient room temperature), storage capacity/free space, and ADB route.

Capture pauses if Android reports battery health `OVERHEAT` (3), another unhealthy battery state, or the battery reaches the 50°C hard limit. Android reports `GOOD` as 2; a `GOOD` phone near 45°C keeps capturing. After a pause, it resumes when health is `GOOD` and temperature is at most 48°C. If health is unavailable, temperature controls the same 50/48°C fallback; if both readings are unavailable, capture pauses. The daemon keeps reporting battery status and checks for recovery every ten seconds. The phone's last recordings remain available in history during a pause. [Android BatteryManager constants](https://developer.android.com/reference/android/os/BatteryManager#BATTERY_HEALTH_OVERHEAT)

The viewer API used is `POST /api/phones/{id}/sessions`, `POST /api/phones/{id}/segments?session_id=…&segment_key=…&captured_at=…&remote_path=…`, `POST /api/phones/{id}/sessions/{session_id}/stop`, `GET /api/phones/{id}/settings`, `POST /api/phones/{id}/status`, and `POST /api/phones/{id}/archive-prune`. Write calls send `X-Phone-Token`. The viewer can re-fetch older clips from `remote_path` through the reported ADB route after its live TS cache rotates out.

## Scrcpy protocol bridge

`scrcpy_bridge.py` is a single-encoder video/control bridge for either device ID. It launches the pinned scrcpy 4.1 server on the selected Android device, receives its H.264 packets with their original presentation timestamps, and serves independent reconnectable MPEG-TS streams at `$XDG_RUNTIME_DIR/phone-capture/<device-id>.video.sock` for archive and `<device-id>.live.sock` for RTSP. Slow archive processing cannot block live video. JSON lines sent to `<device-id>.control.sock` drive the same scrcpy server's control channel (tap, touch down/move/up, swipe, key, or text). All Unix sockets are mode 0600 in a mode 0700 directory.
The bridge also requires Python PyAV (`import av`) for MPEG-TS muxing.

The bridge works when the phone uses a remote ADB server. The remote host may bind `adb forward` to its own loopback, so the bridge instead starts two tiny Android `app_process` relays over binary `adb shell -T` channels. Both relays connect to the scrcpy server's abstract video/control sockets. They do not start another encoder. The relay source is `scrcpy_relay/AbstractRelay.java`; the build script compiles a temporary DEX JAR with Android SDK platform 34 and build-tools 34.0.0. Set `ANDROID_HOME` if the SDK is outside `~/Android/Sdk`. The JAR stays under `.tools/` and is excluded from Git.

Example with the remote ADB server (replace the serial and server JAR path as needed):

```bash
./install_scrcpy.sh
python3 scrcpy_bridge.py --device-id pixel-4-xl --serial <adb-serial> \
  --adb-server-socket tcp:192.0.2.10:5037
```

The process prints `READY <video.sock> <live.sock> <control.sock>` after both relays connect. Separate FFmpeg processes consume the archive and live sockets without starting another phone encoder. On exit the bridge stops the scrcpy server, closes its Unix sockets, and removes its temporary JARs from Android. Use the daemon's thermal guard before starting real-device capture; direct use of the bridge does not add thermal policy.
