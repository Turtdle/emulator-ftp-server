"""
ADB FTP Bridge - Exposes Android emulator filesystem via FTP using ADB as transport.
Connect WinSCP to ftp://127.0.0.1:2123 (no username/password)
"""

import logging
import os
import re
import stat
import time
import subprocess
import tempfile
import traceback
import threading
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.filesystems import AbstractedFS

# ── Configuration ─────────────────────────────────────────────────────────────
ADB = r"Z:\MuMuPlayerGlobal\nx_main\adb.exe"   # Path to adb.exe
DEVICE = "192.168.4.163:5555"                   # ADB device address
FTP_PORT = 2123                                  # FTP port to listen on
LOG_FILE = r"Z:\MuMuPlayerGlobal\ftp_server.log"
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger("adbftp")


_ADB_DEAD_MARKERS = ("cannot connect to daemon", "daemon not running", "failed to start daemon", "no devices/emulators found")

_adb_lock = threading.Lock()
_adb_healthy = threading.Event()
_adb_healthy.set()


def _adb_reconnect():
    log.warning("ADB daemon lost — reconnecting...")
    _adb_healthy.clear()
    subprocess.run([ADB, "kill-server"], capture_output=True)
    time.sleep(1)
    subprocess.run([ADB, "start-server"], capture_output=True)
    time.sleep(1)
    subprocess.run([ADB, "connect", DEVICE], capture_output=True)
    time.sleep(1)
    subprocess.run([ADB, "-s", DEVICE, "root"], capture_output=True)
    time.sleep(2)
    _adb_healthy.set()
    log.warning("ADB reconnect done")


def _adb_keepalive():
    """Background thread: ping ADB every 5s and reconnect if dead."""
    while True:
        time.sleep(5)
        try:
            result = subprocess.run(
                [ADB, "-s", DEVICE, "shell", "echo ok"],
                capture_output=True, text=True, timeout=8
            )
            if any(m in result.stdout + result.stderr for m in _ADB_DEAD_MARKERS):
                _adb_reconnect()
        except Exception as e:
            log.warning("ADB keepalive error: %s — reconnecting", e)
            _adb_reconnect()


def adb(*args, capture=True, _retry=True):
    # Wait for keepalive thread to finish any ongoing reconnect (max 15s)
    _adb_healthy.wait(timeout=15)
    cmd = [ADB, "-s", DEVICE] + list(args)
    if capture:
        result = subprocess.run(cmd, capture_output=True, text=True)
        combined = result.stdout + result.stderr
        if any(m in combined for m in _ADB_DEAD_MARKERS):
            log.warning("ADB %s -> daemon dead, reconnecting. stderr=%s", args, result.stderr.strip())
            if _retry:
                _adb_reconnect()
                return adb(*args, capture=capture, _retry=False)
        elif result.returncode != 0 and result.stderr:
            log.warning("ADB %s -> rc=%d stderr=%s", args, result.returncode, result.stderr.strip())
        return result.stdout, result.stderr, result.returncode
    else:
        result = subprocess.run(cmd)
        if result.returncode != 0:
            log.warning("ADB %s -> rc=%d", args, result.returncode)
        return result


class AdbFS(AbstractedFS):

    def validpath(self, path):
        return True

    def _android_path(self, ftppath):
        """Convert FTP path (rooted at /sdcard) to Android absolute path."""
        # Resolve relative paths against CWD first
        ftppath = self.ftpnorm(ftppath).replace("\\", "/")
        android = "/sdcard" + ftppath if ftppath != "/" else "/sdcard"
        return android

    def listdir(self, path):
        android = self._android_path(path)
        out, _, _ = adb("shell", f"ls '{android}' 2>/dev/null")
        return [f for f in out.strip().splitlines() if f]

    def listdirinfo(self, path):
        return self.listdir(path)

    def isfile(self, path):
        android = self._android_path(path)
        out, _, rc = adb("shell", f"[ -f '{android}' ] && echo yes || echo no")
        return out.strip() == "yes"

    def isdir(self, path):
        android = self._android_path(path)
        out, _, rc = adb("shell", f"[ -d '{android}' ] && echo yes || echo no")
        return out.strip() == "yes"

    def islink(self, path):
        return False

    def getsize(self, path):
        android = self._android_path(path)
        out, _, _ = adb("shell", f"stat -c %s '{android}' 2>/dev/null")
        try:
            return int(out.strip())
        except ValueError:
            return 0

    def getmtime(self, path):
        android = self._android_path(path)
        out, _, _ = adb("shell", f"stat -c %Y '{android}' 2>/dev/null")
        try:
            return float(out.strip())
        except ValueError:
            return time.time()

    def realpath(self, path):
        return path

    def lexists(self, path):
        android = self._android_path(path)
        out, _, _ = adb("shell", f"[ -e '{android}' ] && echo yes || echo no")
        return out.strip() == "yes"

    def stat(self, path):
        android = self._android_path(path)
        out, _, _ = adb("shell", f"stat -c '%f %s %Y' '{android}' 2>/dev/null")
        parts = out.strip().split()
        try:
            mode_hex = int(parts[0], 16)
            size = int(parts[1])
            mtime = float(parts[2])

            class StatResult:
                st_mode = mode_hex
                st_ino = 0
                st_dev = 0
                st_nlink = 1
                st_uid = 0
                st_gid = 0
                st_size = size
                st_atime = mtime
                st_mtime = mtime
                st_ctime = mtime
            return StatResult()
        except (ValueError, IndexError):
            class FallbackStat:
                st_mode = stat.S_IFDIR | 0o755 if self.isdir(path) else stat.S_IFREG | 0o644
                st_ino = 0
                st_dev = 0
                st_nlink = 1
                st_uid = 0
                st_gid = 0
                st_size = 0
                st_atime = st_mtime = st_ctime = time.time()
            return FallbackStat()

    def lstat(self, path):
        return self.stat(path)

    def open(self, filename, mode):
        android = self._android_path(filename)
        try:
            if mode == "rb":
                log.info("DOWNLOAD start: %s -> temp", android)
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".adbdl")
                tmp.close()
                result = subprocess.run(
                    [ADB, "-s", DEVICE, "pull", android, tmp.name],
                    capture_output=True, text=True
                )
                log.info("DOWNLOAD adb pull rc=%d stdout=%s stderr=%s",
                         result.returncode, result.stdout.strip(), result.stderr.strip())
                tmp_size = os.path.getsize(tmp.name)
                log.info("DOWNLOAD temp file size: %d bytes", tmp_size)
                return open(tmp.name, "rb")
            else:
                # Upload: wb (fresh), r+b (resume), ab (append)
                log.info("UPLOAD start (mode=%s): temp -> %s", mode, android)
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".adbul")
                tmp.close()
                if "r+" in mode:
                    # Resume upload — pull existing file first so seek works
                    subprocess.run([ADB, "-s", DEVICE, "pull", android, tmp.name],
                                   capture_output=True)
                self._pending_uploads = getattr(self, "_pending_uploads", {})
                self._pending_uploads[tmp.name] = android
                return open(tmp.name, mode)
        except Exception:
            log.error("open() exception:\n%s", traceback.format_exc())
            raise

    def chdir(self, path):
        self.cwd = self.fs2ftp(path)

    def mkdir(self, path):
        android = self._android_path(path)
        adb("shell", f"mkdir -p '{android}'")

    def rmdir(self, path):
        android = self._android_path(path)
        adb("shell", f"rmdir '{android}'")

    def remove(self, path):
        android = self._android_path(path)
        adb("shell", f"rm -f '{android}'")

    def rename(self, src, dst):
        android_src = self._android_path(src)
        android_dst = self._android_path(dst)
        adb("shell", f"mv '{android_src}' '{android_dst}'")

    def chmod(self, path, mode):
        pass

    def ftp2fs(self, ftppath):
        return ftppath

    def fs2ftp(self, fspath):
        return fspath

    # Strip ANSI escape codes
    _ansi_re = re.compile(r'\x1b\[[0-9;]*m')
    # Match ls -la lines: permissions nlinks owner group size date time name
    _ls_re = re.compile(
        r'^([d\-lscbp][rwxsStT\-]{9})\s+\d+\s+\S+\s+\S+\s+(\d+)\s+'
        r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})\s+(.+)$'
    )

    def _ls_la(self, basedir):
        """Run ls -la once and return a dict of name -> (is_dir, size, perms, ts)."""
        android = self._android_path(basedir).rstrip("/") + "/"
        out, _, _ = adb("shell", f"ls -la '{android}' 2>/dev/null")
        info = {}
        for line in out.splitlines():
            line = self._ansi_re.sub('', line).strip()
            m = self._ls_re.match(line)
            if m:
                perms, size, ts, name = m.groups()
                info[name] = (perms[0] == 'd', int(size), perms, ts)
        return info

    def format_list(self, basedir, listing, ignore_err=True):
        info = self._ls_la(basedir)
        for name in listing:
            if name in info:
                is_dir, size, perms, ts = info[name]
                perm = "drwxr-xr-x" if is_dir else "-rw-r--r--"
                yield (f"{perm}  1 android android {size:>12} {ts} {name}\r\n").encode()
            else:
                yield (f"-rw-r--r--  1 android android            0 Jan  1 00:00 {name}\r\n").encode()

    def format_mlsx(self, basedir, listing, perms, facts, ignore_err=True):
        info = self._ls_la(basedir)
        for name in listing:
            if name in info:
                is_dir, size, _, ts = info[name]
                ftype = "dir" if is_dir else "file"
                # Convert "2025-09-05 21:39" to "20250905213900"
                ts_clean = ts.replace("-", "").replace(" ", "").replace(":", "") + "00"
                yield (f"Type={ftype};Size={size};Modify={ts_clean}; {name}\r\n").encode()
            else:
                yield (f"Type=file;Size=0;Modify=19700101000000; {name}\r\n").encode()


class AdbFTPHandler(FTPHandler):
    abstracted_fs = AdbFS

    def on_file_sent(self, file):
        log.info("DOWNLOAD complete: %s", file)
        if file and os.path.exists(file):
            try:
                os.unlink(file)
            except Exception:
                log.warning("Could not delete temp file: %s", file)

    def on_incomplete_file_sent(self, file):
        log.error("DOWNLOAD incomplete/aborted: %s", file)
        if file and os.path.exists(file):
            try:
                os.unlink(file)
            except Exception:
                pass

    def on_file_received(self, file):
        log.info("UPLOAD complete: %s", file)
        fs = self.fs
        if hasattr(fs, "_pending_uploads") and file in fs._pending_uploads:
            android_path = fs._pending_uploads.pop(file)
            log.info("UPLOAD pushing %s -> %s", file, android_path)
            result = subprocess.run(
                [ADB, "-s", DEVICE, "push", file, android_path],
                capture_output=True, text=True
            )
            log.info("UPLOAD push rc=%d stdout=%s stderr=%s",
                     result.returncode, result.stdout.strip(), result.stderr.strip())
            # Verify the file actually landed
            verify, _, _ = adb("shell", f"ls -la '{android_path}' 2>/dev/null")
            log.info("UPLOAD verify ls: %s", verify.strip() or "(empty — file not found!)")
        try:
            os.unlink(file)
        except Exception:
            log.warning("Could not delete temp file: %s", file)

    def on_incomplete_file_received(self, file):
        log.error("UPLOAD incomplete/aborted: %s", file)
        try:
            os.unlink(file)
        except Exception:
            pass

    def handle_error(self):
        log.error("Unhandled handler exception:\n%s", traceback.format_exc())


def main():
    authorizer = DummyAuthorizer()
    authorizer.add_anonymous("/", perm="elradfmwMT")

    handler = AdbFTPHandler
    handler.authorizer = authorizer
    handler.passive_ports = range(60000, 60100)
    handler.timeout = 7200       # 2 hour timeout on control connection
    handler.dtp_handler.ac_out_buffer_size = 1 << 20  # 1MB send buffer

    print(f"Starting ADB FTP bridge on ftp://127.0.0.1:{FTP_PORT}")
    print(f"Device: {DEVICE}")
    print(f"Connect WinSCP to: ftp://127.0.0.1:{FTP_PORT} (no login required)")
    print("Press Ctrl+C to stop.")

    # Start ADB keepalive thread
    t = threading.Thread(target=_adb_keepalive, daemon=True)
    t.start()
    log.info("ADB keepalive thread started")

    FTPServer.allow_reuse_address = True
    server = FTPServer(("127.0.0.1", FTP_PORT), handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
