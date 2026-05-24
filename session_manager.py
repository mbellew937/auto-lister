import subprocess
import os
import socket
import sys
import time
import psutil
from typing import Dict, Optional

DESKTOP_CONFIG = {
    "xvfb_res": "1600x900x24",
    "window_size": "1600,900",
}

# Phone-class browser metrics: the VNC framebuffer stays physical-resolution
# while Chromium reports the CSS viewport separately at DPR 3.
MOBILE_SCREEN_WIDTH = int(os.environ.get("AUTO_MARKETPLACE_MOBILE_SCREEN_WIDTH", "1080"))
MOBILE_SCREEN_HEIGHT = int(os.environ.get("AUTO_MARKETPLACE_MOBILE_SCREEN_HEIGHT", "1960"))
MOBILE_VIEWPORT_WIDTH = int(os.environ.get("AUTO_MARKETPLACE_MOBILE_VIEWPORT_WIDTH", "360"))
MOBILE_VIEWPORT_HEIGHT = int(os.environ.get("AUTO_MARKETPLACE_MOBILE_VIEWPORT_HEIGHT", "653"))
MOBILE_CONFIG = {
    "xvfb_res": f"{MOBILE_SCREEN_WIDTH}x{MOBILE_SCREEN_HEIGHT}x24",
    "window_size": f"{MOBILE_SCREEN_WIDTH},{MOBILE_SCREEN_HEIGHT}",
}

MAX_IDLE_SECONDS = int(os.environ.get("AUTO_MARKETPLACE_MAX_IDLE_SECONDS", str(30 * 60)))
MAX_SESSIONS = int(os.environ.get("AUTO_MARKETPLACE_MAX_SESSIONS", "6"))

class SessionManager:
    def __init__(
        self,
        app_dir: str,
        data_dir: Optional[str] = None,
        novnc_dir: Optional[str] = None,
        python_executable: Optional[str] = None,
    ):
        self.app_dir = app_dir
        self.data_dir = data_dir or app_dir
        self.novnc_dir = novnc_dir or os.path.join(app_dir, "noVNC")
        self.python_executable = python_executable or os.environ.get("PYTHON_EXECUTABLE", sys.executable)
        self.sessions: Dict[str, dict] = {}
        self.next_display = 100

    def _session_key(self, user_id: str, device: str) -> str:
        return f"{user_id}:{device}"

    def _wait_for_x_display(self, display_num: int, proc: subprocess.Popen, timeout: float = 6) -> bool:
        display_socket = f"/tmp/.X11-unix/X{display_num}"
        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                return False
            if os.path.exists(display_socket):
                return True
            time.sleep(0.1)
        return False

    def _wait_for_tcp(self, host: str, port: int, proc: subprocess.Popen, timeout: float = 8) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                return False
            try:
                with socket.create_connection((host, port), timeout=0.25):
                    return True
            except OSError:
                time.sleep(0.15)
        return False

    def _terminate_procs(self, procs):
        for proc in procs:
            try:
                p = psutil.Process(proc.pid)
                for child in p.children(recursive=True):
                    child.terminate()
                p.terminate()
                try:
                    p.wait(timeout=2)
                except psutil.TimeoutExpired:
                    p.kill()
            except Exception:
                pass

    def get_or_create_session(self, user_id: str, device: str = "desktop") -> dict:
        self._prune_sessions()
        key = self._session_key(user_id, device)
        if key in self.sessions:
            if self._is_session_alive(key):
                self._touch_session(key)
                return self.sessions[key]
            else:
                self.stop_session(user_id, device)

        display_num = self.next_display
        self.next_display += 1

        vnc_port = 5900 + display_num
        ws_port  = 6080 + display_num
        cdp_port = 9200 + display_num

        cfg = MOBILE_CONFIG if device == "mobile" else DESKTOP_CONFIG
        profile_dir = os.path.join(self.data_dir, "profiles", f"{user_id}-{device}")
        os.makedirs(profile_dir, exist_ok=True)

        env = os.environ.copy()
        env["DISPLAY"] = f":{display_num}"
        env["AUTO_MARKETPLACE_APP_DIR"] = self.app_dir
        env["AUTO_MARKETPLACE_DATA_DIR"] = self.data_dir
        env["AUTO_MARKETPLACE_NOVNC_DIR"] = self.novnc_dir

        xvfb_proc = subprocess.Popen([
            "Xvfb", f":{display_num}", "-screen", "0", cfg["xvfb_res"]
        ])
        if not self._wait_for_x_display(display_num, xvfb_proc):
            self._terminate_procs([xvfb_proc])
            raise RuntimeError(f"Xvfb display :{display_num} did not become ready.")

        openbox_proc = subprocess.Popen(["openbox-session"], env=env)
        vnc_proc = subprocess.Popen([
            "x11vnc", "-display", f":{display_num}", "-forever", "-shared", "-nopw",
            "-quiet", "-listen", "localhost", "-rfbport", str(vnc_port)
        ])
        if not self._wait_for_tcp("127.0.0.1", vnc_port, vnc_proc):
            self._terminate_procs([vnc_proc, openbox_proc, xvfb_proc])
            raise RuntimeError(f"x11vnc did not open localhost:{vnc_port}.")

        ws_proc = subprocess.Popen([
            os.path.join(self.novnc_dir, "utils", "novnc_proxy"),
            "--vnc", f"localhost:{vnc_port}", "--listen", str(ws_port)
        ])
        if not self._wait_for_tcp("127.0.0.1", ws_port, ws_proc):
            self._terminate_procs([ws_proc, vnc_proc, openbox_proc, xvfb_proc])
            raise RuntimeError(f"noVNC proxy did not open localhost:{ws_port}.")

        browser_proc = subprocess.Popen([
            self.python_executable, "-u",
            os.path.join(self.app_dir, "launch_browser.py"),
            user_id, f":{display_num}", str(cdp_port), device
        ], env=env)

        session_data = {
            "display": display_num,
            "vnc_port": vnc_port,
            "ws_port": ws_port,
            "cdp_port": cdp_port,
            "profile_dir": profile_dir,
            "device": device,
            "created_at": time.time(),
            "last_used": time.time(),
            "client_count": 0,
            "procs": [xvfb_proc, openbox_proc, vnc_proc, ws_proc, browser_proc],
        }
        self.sessions[key] = session_data
        self._prune_sessions(exclude_keys={key})
        return session_data

    def mark_connected(self, user_id: str, device: str = "desktop"):
        key = self._session_key(user_id, device)
        session = self.sessions.get(key)
        if not session:
            return
        session["client_count"] = session.get("client_count", 0) + 1
        self._touch_session(key)

    def mark_disconnected(self, user_id: str, device: str = "desktop"):
        key = self._session_key(user_id, device)
        session = self.sessions.get(key)
        if not session:
            return
        session["client_count"] = max(0, session.get("client_count", 0) - 1)
        self._touch_session(key)
        self._prune_sessions()

    def _is_session_alive(self, key: str) -> bool:
        session = self.sessions.get(key)
        if not session:
            return False
        return all(p.poll() is None for p in session["procs"])

    def _touch_session(self, key: str):
        session = self.sessions.get(key)
        if session:
            session["last_used"] = time.time()

    def _prune_sessions(self, exclude_keys=None):
        exclude_keys = exclude_keys or set()
        now = time.time()

        for key, session in list(self.sessions.items()):
            if key in exclude_keys:
                continue
            if not self._is_session_alive(key):
                user_id, device = key.split(":", 1)
                self.stop_session(user_id, device)
                continue
            idle_for = now - session.get("last_used", session.get("created_at", now))
            if session.get("client_count", 0) == 0 and idle_for > MAX_IDLE_SECONDS:
                user_id, device = key.split(":", 1)
                self.stop_session(user_id, device)

        def removable_keys():
            return [
                key for key, session in self.sessions.items()
                if key not in exclude_keys and session.get("client_count", 0) == 0
            ]

        while len(self.sessions) > MAX_SESSIONS:
            candidates = removable_keys()
            if not candidates:
                break
            oldest_key = min(
                candidates,
                key=lambda item: self.sessions[item].get("last_used", 0),
            )
            user_id, device = oldest_key.split(":", 1)
            self.stop_session(user_id, device)

    def stop_session(self, user_id: str, device: str = "desktop"):
        key = self._session_key(user_id, device)
        session = self.sessions.pop(key, None)
        if session:
            self._terminate_procs(session["procs"])
