"""
winapi.py - plain-ctypes Windows helpers (windows, processes, focus).

Shared by recorder.py and planner.py. Previously planner imported recorder just to get
these, which also ran recorder's module-level side effects (host-app detection, etc.).
No COM here, so every function is safe to call from any thread.

Windows only.
"""
import ctypes
from ctypes import wintypes

import psutil


def _set_dpi_aware():
    # pynput, UI Automation and screenshots must all use the same pixel coordinates.
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass  # already set (or unsupported): fine


_set_dpi_aware()

user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
GW_OWNER = 4
DWMWA_CLOAKED = 14
SW_MINIMIZE = 6
SW_RESTORE = 9
WM_CLOSE = 0x0010
IGNORED_CLASSES = {"Progman", "WorkerW"}
FRAME_HOST = "applicationframehost.exe"

user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
user32.GetForegroundWindow.restype = wintypes.HWND
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.EnumChildWindows.argtypes = [wintypes.HWND, WNDENUMPROC, wintypes.LPARAM]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]


# ---------------------------------------------------------------------------
# reading windows
# ---------------------------------------------------------------------------
def window_text(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def window_class(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def hwnd_pid(hwnd):
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def foreground_window():
    hwnd = user32.GetForegroundWindow()
    return int(hwnd) if hwnd else 0


def window_rect(hwnd):
    """(left, top, right, bottom) in screen pixels."""
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def list_windows():
    """Visible, un-cloaked, top-level 'real' windows: {hwnd: info}."""
    found = {}

    def cb(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            if user32.GetWindow(hwnd, GW_OWNER):
                return True
            if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
                return True
            if user32.GetWindowTextLengthW(hwnd) == 0:
                return True
            cloaked = ctypes.c_int(0)
            dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
            if cloaked.value:
                return True
            cls = window_class(hwnd)
            if cls in IGNORED_CLASSES:
                return True
            found[int(hwnd)] = {
                "title": window_text(hwnd),
                "class": cls,
                "pid": hwnd_pid(hwnd),
                "minimized": bool(user32.IsIconic(hwnd)),
            }
        except Exception:
            pass
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return found


# ---------------------------------------------------------------------------
# processes
# ---------------------------------------------------------------------------
def proc_name(pid):
    try:
        return psutil.Process(pid).name()
    except Exception:
        return "unknown"


def proc_exe(pid):
    try:
        return psutil.Process(pid).exe()
    except Exception:
        return ""


def child_pid(hwnd, frame_pid):
    """Store apps live inside a frame window owned by ApplicationFrameHost; the real app is a child."""
    pids = []

    def cb(child, _lparam):
        p = hwnd_pid(child)
        if p and p != frame_pid:
            pids.append(p)
        return True

    user32.EnumChildWindows(hwnd, WNDENUMPROC(cb), 0)
    return pids[0] if pids else frame_pid


# Keyed by (hwnd, pid): Windows reuses window handles, so a bare hwnd could return a stale app name.
_app_cache = {}


def resolve_app(hwnd, pid):
    """Returns (app_name, exe_path, resolved)."""
    key = (hwnd, pid)
    cached = _app_cache.get(key)
    if cached:
        return cached[0], cached[1], True
    name, exe, resolved = proc_name(pid), proc_exe(pid), True
    if name.lower() == FRAME_HOST:
        pid2 = child_pid(hwnd, pid)
        if pid2 != pid:
            name, exe = proc_name(pid2), proc_exe(pid2)
        else:
            resolved = False
    if resolved:
        if len(_app_cache) > 2000:
            _app_cache.clear()
        _app_cache[key] = (name, exe)
    return name, exe, resolved


def app_of_hwnd(hwnd):
    name, exe, _ = resolve_app(hwnd, hwnd_pid(hwnd))
    return name, exe


def find_host_apps():
    """Programs that host THIS assistant (e.g. the terminal / VS Code). Ignored while learning."""
    skip = {"explorer.exe", "services.exe", "wininit.exe", "system", "svchost.exe",
            "winlogon.exe", "smss.exe", "csrss.exe"}
    names = []
    try:
        for p in psutil.Process().parents():
            if p.name().lower() not in skip:
                names.append(p.name())
    except Exception:
        pass
    return names


# ---------------------------------------------------------------------------
# controlling windows
# ---------------------------------------------------------------------------
def bring_to_front(hwnd):
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    user32.keybd_event(0x12, 0, 0, 0)      # tap Alt: lets SetForegroundWindow succeed
    user32.SetForegroundWindow(hwnd)
    user32.keybd_event(0x12, 0, 2, 0)


def close_window(hwnd):
    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)


def minimize_window(hwnd):
    user32.ShowWindow(hwnd, SW_MINIMIZE)
