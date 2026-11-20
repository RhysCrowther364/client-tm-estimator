"""Standalone Windows Desktop Launcher for RWS Patent TM Suitability & RTE Estimator.

Starts the embedded local calculation engine on an available loopback port (no console window),
opens a dedicated chromeless Windows Desktop App window, and shuts down cleanly when closed.
"""
from __future__ import annotations

import io
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

import uvicorn

# Guard against None stdout/stderr in PyInstaller --noconsole Windows GUI mode
if sys.stdout is None:
    sys.stdout = io.StringIO()
if sys.stderr is None:
    sys.stderr = io.StringIO()

from app.config import DATA_DIR
from app.main import app


def _find_free_port(preferred: int = 8765) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(("127.0.0.1", preferred)) != 0:
            return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_for_server(url: str, timeout_s: float = 12.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/api/settings", timeout=1.0) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.15)
    return False


def _find_app_host_browser() -> str | None:
    """Locate Microsoft Edge (built into Windows 10/11) or Chrome for native --app chromeless desktop mode."""
    candidates = [
        shutil.which("msedge"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe"),
        shutil.which("chrome"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for path in candidates:
        if path and Path(path).exists():
            return str(path)
    return None


def _run_fallback_tk_controller(url: str) -> None:
    """Fallback compact RWS Desktop controller if neither Edge nor Chrome is found in standard paths."""
    import tkinter as tk

    webbrowser.open(url)
    root = tk.Tk()
    root.title("RWS Patent TM Estimator")
    root.geometry("380x165")
    root.configure(bg="#38006B")
    root.resizable(False, False)

    lbl_title = tk.Label(
        root,
        text="rws • Patent TM Estimator",
        fg="#FFFFFF",
        bg="#38006B",
        font=("Segoe UI", 13, "bold"),
    )
    lbl_title.pack(pady=(18, 4))

    lbl_sub = tk.Label(
        root,
        text=f"Local engine running at {url}\nClose this window when finished.",
        fg="#E2D9F3",
        bg="#38006B",
        font=("Segoe UI", 9),
    )
    lbl_sub.pack(pady=(0, 14))

    btn_open = tk.Button(
        root,
        text="Open Interface",
        command=lambda: webbrowser.open(url),
        bg="#E60050",
        fg="#FFFFFF",
        activebackground="#C40044",
        activeforeground="#FFFFFF",
        relief="flat",
        padx=16,
        pady=5,
        font=("Segoe UI", 9, "bold"),
    )
    btn_open.pack()
    root.mainloop()


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    port = _find_free_port(8770)
    url = f"http://127.0.0.1:{port}"

    config = uvicorn.Config(
        app=app,
        host="127.0.0.1",
        port=port,
        log_config=None,
        access_log=False,
    )
    server = uvicorn.Server(config)

    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    if not _wait_for_server(url):
        sys.exit(1)

    browser_exe = _find_app_host_browser()
    if browser_exe:
        profile_dir = DATA_DIR / "desktop_window_profile"
        profile_dir.mkdir(parents=True, exist_ok=True)
        args = [
            browser_exe,
            f"--app={url}",
            f"--user-data-dir={profile_dir}",
            "--window-size=1440,920",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
        ]
        proc = subprocess.Popen(args)
        try:
            proc.wait()
        finally:
            server.should_exit = True
            server_thread.join(timeout=3.0)
    else:
        try:
            _run_fallback_tk_controller(url)
        finally:
            server.should_exit = True
            server_thread.join(timeout=3.0)


if __name__ == "__main__":
    main()
