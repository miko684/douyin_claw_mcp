"""Local browser dashboard for starting and observing the Douyin MCP server."""

from __future__ import annotations

import json
import mimetypes
import os
import subprocess
import sys
import threading
from urllib.parse import unquote, urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mcp_auth import AuthManager
from mcp_stats import StatsStore


ROOT = Path(__file__).resolve().parent
_BUNDLED_PROJECT_ROOT = ROOT / "DouYin_Spider"
PROJECT_ROOT = Path(os.getenv("DOUYIN_PROJECT_ROOT") or _BUNDLED_PROJECT_ROOT).resolve()
STATS_FILE = ROOT / "work" / "mcp_stats.json"
RESULTS_ROOT = STATS_FILE.parent.resolve()
STATS = StatsStore(str(STATS_FILE))
PROCESS: subprocess.Popen | None = None
LOGIN_PROCESS: subprocess.Popen | None = None
PROCESS_LOCK = threading.Lock()


def _mcp_lock_is_free() -> bool:
    """Probe whether another MCP process currently owns the singleton lock."""
    lock_path = STATS_FILE.with_name("mcp_server.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    acquired = False
    try:
        if lock_path.stat().st_size == 0:
            handle.write("0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            acquired = True
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (OSError, IOError):
        acquired = False
    finally:
        handle.close()
    return acquired


def process_running() -> bool:
    return (PROCESS is not None and PROCESS.poll() is None) or not _mcp_lock_is_free()


def login_running() -> bool:
    return LOGIN_PROCESS is not None and LOGIN_PROCESS.poll() is None


def start_login_if_needed(env: dict[str, str]) -> bool:
    global LOGIN_PROCESS
    if login_running():
        return False
    auth_status = AuthManager(str(PROJECT_ROOT)).status()
    if auth_status["normal_login"] and auth_status["live_cookie_available"]:
        return False
    LOGIN_PROCESS = subprocess.Popen(
        [sys.executable, str(ROOT / "run_local.py"), "login_worker.py"],
        cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return True


def start_mcp() -> bool:
    global PROCESS
    with PROCESS_LOCK:
        if process_running():
            return False
        # Codex normally owns the stdio MCP process. Never create a second
        # server when that process already holds the singleton lock.
        if not _mcp_lock_is_free():
            return False
        env = dict(os.environ)
        env["DOUYIN_PROJECT_ROOT"] = str(PROJECT_ROOT)
        env["DOUYIN_STATS_FILE"] = str(STATS_FILE)
        env["DOUYIN_OPEN_DASHBOARD"] = "0"
        PROCESS = subprocess.Popen(
            [sys.executable, str(ROOT / "run_local.py"), "mcp_server.py"],
            cwd=str(ROOT), env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        start_login_if_needed(env)
        return True


def stop_mcp() -> bool:
    global PROCESS, LOGIN_PROCESS
    with PROCESS_LOCK:
        if login_running():
            LOGIN_PROCESS.terminate()
            try:
                LOGIN_PROCESS.wait(timeout=5)
            except subprocess.TimeoutExpired:
                LOGIN_PROCESS.kill()
        LOGIN_PROCESS = None
        if PROCESS is None or PROCESS.poll() is not None:
            PROCESS = None
            return False
        if os.name == "nt":
            # run_local.py may be a launcher whose MCP child survives a
            # normal terminate(); stop the whole process tree so stale MCP
            # workers cannot keep crawling or writing duplicate results.
            subprocess.run(
                ["taskkill", "/PID", str(PROCESS.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            PROCESS.terminate()
            try:
                PROCESS.wait(timeout=5)
            except subprocess.TimeoutExpired:
                PROCESS.kill()
        PROCESS = None
        STATS.set_status("stopped")
        return True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def _json(self, status: int, payload: dict):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/":
            raw = (ROOT / "dashboard.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if path == "/api/status":
            data = STATS.read()
            data["process_running"] = process_running()
            data["login_running"] = login_running()
            auth_status = AuthManager(str(PROJECT_ROOT)).status()
            data["normal_login"] = auth_status["normal_login"]
            data["live_cookie_available"] = auth_status["live_cookie_available"]
            data["project_root"] = str(PROJECT_ROOT)
            self._json(200, data)
            return
        if path.startswith("/api/artifacts/"):
            relative = unquote(path[len("/api/artifacts/"):])
            target = (RESULTS_ROOT / relative).resolve()
            if RESULTS_ROOT not in target.parents or not target.is_file():
                self._json(404, {"error": "artifact_not_found"})
                return
            raw = target.read_bytes()
            content_type = {
                ".json": "application/json; charset=utf-8",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
                ".webp": "image/webp",
                ".gif": "image/gif",
                ".avif": "image/avif",
            }.get(target.suffix.lower()) or mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Disposition", f'inline; filename="{target.name}"')
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        self._json(404, {"error": "not_found"})

    def do_POST(self):
        path = urlsplit(self.path).path
        if path == "/api/start":
            started = start_mcp()
            self._json(200, {"started": started, "login_running": login_running()})
            return
        if path == "/api/stop":
            self._json(200, {"stopped": stop_mcp()})
            return
        if path == "/api/clear":
            try:
                result = STATS.clear()
            except (OSError, ValueError) as exc:
                self._json(500, {"error": "clear_failed", "message": str(exc)})
                return
            self._json(200, {"cleared": True, **result})
            return
        if path.startswith("/api/clear/"):
            kind = unquote(path[len("/api/clear/"):]).strip("/")
            try:
                result = STATS.clear(kind)
            except ValueError as exc:
                self._json(400, {"error": "unsupported_clear_kind", "message": str(exc)})
                return
            except OSError as exc:
                self._json(500, {"error": "clear_failed", "message": str(exc)})
                return
            self._json(200, {"cleared": True, **result})
            return
        self._json(404, {"error": "not_found"})


def main():
    port = int(os.getenv("DOUYIN_DASHBOARD_PORT", "8765"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    if not process_running():
        STATS.set_status("stopped")
    print(f"DouYin MCP 控制台: http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        stop_mcp()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
