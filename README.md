# emulator-ftp-server

A Python FTP server that bridges WinSCP (or any FTP client) to an Android emulator's filesystem using ADB as the transport layer. No app installation required on the emulator — it works entirely via `adb pull`/`adb push`.

## Features

- Browse, upload, and download files on the Android emulator from WinSCP
- Anonymous FTP access (no login required)
- ADB keepalive — automatically reconnects if the ADB daemon dies mid-session
- Fast directory listings using a single `ls -la` call per directory
- Full upload/download/rename/delete/mkdir support
- Resume uploads (REST + STOR) supported
- Passive mode FTP (ports 60000–60100)

## Requirements

- Python 3.8+
- `adb.exe` accessible on your machine
- Android emulator running with ADB over TCP (e.g. MuMuPlayer at `192.168.4.163:5555`)
- ADB root access on the emulator (`adb root`)

```
pip install -r requirements.txt
```

## Configuration

Edit the constants at the top of `adb_ftp_server.py`:

```python
ADB    = r"Z:\MuMuPlayerGlobal\nx_main\adb.exe"  # Path to adb.exe
DEVICE = "192.168.4.163:5555"                      # ADB device (IP:port)
FTP_PORT = 2123                                     # FTP port to listen on
LOG_FILE = r"Z:\MuMuPlayerGlobal\ftp_server.log"  # Log file path
```

## Usage

```
python adb_ftp_server.py
```

Then connect WinSCP (or any FTP client) to:

```
ftp://127.0.0.1:2123
```

- **No username or password required** — click "Anonymous login" in WinSCP
- The FTP root maps to `/sdcard` on the Android device
- Passive mode: ports 60000–60100 (make sure these are not firewalled)

## How it works

```
WinSCP  <──FTP──>  adb_ftp_server.py  <──ADB──>  Android emulator
```

- Directory listings run `ls -la` via ADB shell
- Downloads run `adb pull` to a temp file, then stream it to the FTP client
- Uploads stream from the FTP client into a temp file, then run `adb push` when the transfer completes
- A background thread pings the device every 5 seconds and reconnects if the ADB daemon dies

## Tips

- For access to `/sdcard/Android/data` (restricted on Android 11+), bind-mount it as root:
  ```
  adb shell mount --bind /sdcard/Android/data /sdcard/androiddata
  ```
  Then browse `/androiddata` in WinSCP.
- Kill stale Python FTP processes on Windows:
  ```powershell
  Get-Process python | Stop-Process -Force
  ```
