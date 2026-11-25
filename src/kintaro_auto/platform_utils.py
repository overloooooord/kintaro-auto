"""
Cross-platform Windows / Linux / macOS runtime utilities for KINTARO FARM OS.
Ensures seamless Playwright Chromium discovery, UTF-8 console output, and asyncio event loop setup on Windows 10/11.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path


def configure_windows_runtime() -> None:
    """Safely enables UTF-8 output and ProactorEventLoop (required by Playwright on Windows)."""
    if sys.platform == "win32":
        for stream_name in ("stdout", "stderr"):
            stream = getattr(sys, stream_name, None)
            if stream is not None and hasattr(stream, "reconfigure"):
                try:
                    stream.reconfigure(encoding="utf-8", errors="replace")
                except Exception:
                    pass
        try:
            policy = asyncio.get_event_loop_policy()
            proactor_cls = getattr(asyncio, "WindowsProactorEventLoopPolicy", None)
            if proactor_cls is not None and not isinstance(policy, proactor_cls):
                asyncio.set_event_loop_policy(proactor_cls())
        except Exception:
            pass


def find_chromium_executable(explicit: str | None = None) -> str | None:
    """
    Locates an installed Chrome / Chromium / Edge or Playwright browser across Windows, Linux, and macOS.
    Returns `None` if no explicit system binary is found so Playwright uses its own bundled Chromium cleanly.
    """
    if explicit and os.path.isfile(explicit):
        return explicit

    candidates: list[str] = []
    if sys.platform == "win32":
        pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        pf86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        local_app = os.environ.get("LOCALAPPDATA", "")
        candidates.extend(
            [
                os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(pf86, "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(local_app, "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(pf86, "Microsoft", "Edge", "Application", "msedge.exe"),
                os.path.join(pf, "Microsoft", "Edge", "Application", "msedge.exe"),
            ]
        )
        if local_app:
            pw_win = Path(local_app) / "ms-playwright"
            if pw_win.is_dir():
                for c in reversed(sorted(pw_win.glob("chromium-*/chrome-win*/chrome.exe"))):
                    candidates.append(str(c))
    elif sys.platform == "darwin":
        candidates.extend(
            [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Chromium.app/Contents/MacOS/Chromium",
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            ]
        )
        pw_mac = Path.home() / "Library" / "Caches" / "ms-playwright"
        if pw_mac.is_dir():
            for c in reversed(sorted(pw_mac.glob("chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium"))):
                candidates.append(str(c))
    else:
        candidates.extend(
            [
                "/usr/bin/google-chrome",
                "/usr/bin/google-chrome-stable",
                "/usr/bin/chromium",
                "/usr/bin/chromium-browser",
            ]
        )
        pw_lin = Path.home() / ".cache" / "ms-playwright"
        if pw_lin.is_dir():
            for c in reversed(sorted(pw_lin.glob("chromium-*/chrome-linux*/chrome"))):
                candidates.append(str(c))

    for cand in candidates:
        if cand and os.path.isfile(cand):
            return cand

    for bin_name in ("google-chrome", "chromium", "chromium-browser", "chrome", "msedge"):
        found = shutil.which(bin_name)
        if found:
            return found

    return None
