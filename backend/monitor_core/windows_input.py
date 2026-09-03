"""Windows native input for a single, user-visible Chromium slider attempt."""

from __future__ import annotations

import ctypes
import math
import sys
import time
from ctypes import wintypes
from typing import Any


UNSUPPORTED = "unsupported"
NOT_READY = "not_ready"
FAILED = "failed"
DISPATCHED = "dispatched"


_SLIDER_PRESENT_SCRIPT = r"""
return Boolean(
  document.querySelector('#aliyunCaptcha-sliding-wrapper') &&
  document.querySelector('#aliyunCaptcha-sliding-slider')
);
"""


_SLIDER_STATE_SCRIPT = r"""
const wrapper = document.querySelector('#aliyunCaptcha-sliding-wrapper');
const handle = document.querySelector('#aliyunCaptcha-sliding-slider');
const failed = Boolean(document.querySelector(
  '#aliyunCaptcha-sliding-wrapper.fail, #aliyunCaptcha-sliding-slider.fail, ' +
  '#aliyunCaptcha-sliding-left.fail, .aliyunCaptcha .fail, ' +
  '.aliyunCaptcha-sliding-wrapper.fail'
));
const completed = Boolean(document.querySelector(
  '#aliyunCaptcha-sliding-left.success, #aliyunCaptcha-sliding-wrapper.success, ' +
  '#aliyunCaptcha-sliding-slider.success, .aliyunCaptcha .success'
));
const visible = (element) => {
  if (!element) return false;
  const style = getComputedStyle(element);
  const rect = element.getBoundingClientRect();
  return style.display !== 'none' && style.visibility !== 'hidden' &&
    Number(style.opacity || 1) > 0 && rect.width > 0 && rect.height > 0;
};
if (!wrapper || !handle) {
  return {present: false, failed, completed};
}
const wrapperRect = wrapper.getBoundingClientRect();
const handleRect = handle.getBoundingClientRect();
return {
  present: true,
  failed,
  completed,
  visible: visible(wrapper) && visible(handle),
  wrapper: {left: wrapperRect.left, top: wrapperRect.top,
            width: wrapperRect.width, height: wrapperRect.height},
  handle: {left: handleRect.left, top: handleRect.top,
           width: handleRect.width, height: handleRect.height},
  viewport: {width: window.innerWidth, height: window.innerHeight}
};
"""


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [("type", wintypes.DWORD), ("value", _INPUT_UNION)]


_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


class _Win32:
    """Small ctypes adapter kept injectable for deterministic unit tests."""

    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    SW_RESTORE = 9
    INPUT_MOUSE = 0
    INPUT_KEYBOARD = 1
    MOUSEEVENTF_MOVE = 0x0001
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    MOUSEEVENTF_ABSOLUTE = 0x8000
    MOUSEEVENTF_VIRTUALDESK = 0x4000
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_UNICODE = 0x0004
    VK_CONTROL = 0x11
    VK_RETURN = 0x0D
    VK_T = 0x54
    VK_L = 0x4C
    SM_XVIRTUALSCREEN = 76
    SM_YVIRTUALSCREEN = 77
    SM_CXVIRTUALSCREEN = 78
    SM_CYVIRTUALSCREEN = 79

    def __init__(self) -> None:
        self.user32 = ctypes.windll.user32
        self.kernel32 = ctypes.windll.kernel32
        self.kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        self.kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        self.kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
        self.kernel32.Process32FirstW.restype = wintypes.BOOL
        self.kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
        self.kernel32.Process32NextW.restype = wintypes.BOOL
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL
        self.user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user32.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user32.IsWindowVisible.restype = wintypes.BOOL
        self.user32.IsIconic.argtypes = [wintypes.HWND]
        self.user32.IsIconic.restype = wintypes.BOOL
        self.user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.user32.GetWindowRect.restype = wintypes.BOOL
        self.user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.user32.GetClientRect.restype = wintypes.BOOL
        self.user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.ShowWindow.restype = wintypes.BOOL
        self.user32.BringWindowToTop.argtypes = [wintypes.HWND]
        self.user32.BringWindowToTop.restype = wintypes.BOOL
        self.user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        self.user32.SetForegroundWindow.restype = wintypes.BOOL
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        self.user32.GetSystemMetrics.restype = ctypes.c_int
        self.user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user32.GetClassNameW.restype = ctypes.c_int
        self.user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        self.user32.ClientToScreen.restype = wintypes.BOOL
        self.user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
        self.user32.SendInput.restype = wintypes.UINT

    def process_tree(self, root_pid: int) -> set[int]:
        snapshot = self.kernel32.CreateToolhelp32Snapshot(self.TH32CS_SNAPPROCESS, 0)
        if snapshot == self.INVALID_HANDLE_VALUE:
            return {root_pid}
        parents: dict[int, int] = {}
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        try:
            if self.kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
                while True:
                    parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                    if not self.kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                        break
        finally:
            self.kernel32.CloseHandle(snapshot)
        result = {int(root_pid)}
        changed = True
        while changed:
            changed = False
            for pid, parent in parents.items():
                if parent in result and pid not in result:
                    result.add(pid)
                    changed = True
        return result

    def _window_pid(self, hwnd: int) -> int:
        pid = wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)

    def _rect(self, hwnd: int, *, client: bool = False) -> tuple[int, int, int, int]:
        rect = wintypes.RECT()
        getter = self.user32.GetClientRect if client else self.user32.GetWindowRect
        if not getter(hwnd, ctypes.byref(rect)):
            raise OSError("unable to read browser window bounds")
        return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)

    def find_browser_window(self, pids: set[int]) -> int | None:
        matches: list[tuple[int, int]] = []
        @_WNDENUMPROC
        def callback(hwnd: int, _lparam: int) -> bool:
            if self._window_pid(hwnd) in pids and self.user32.IsWindowVisible(hwnd):
                try:
                    left, top, right, bottom = self._rect(hwnd)
                    matches.append((max(0, right - left) * max(0, bottom - top), int(hwnd)))
                except OSError:
                    pass
            return True

        self.user32.EnumWindows(callback, 0)
        return max(matches, default=(0, 0))[1] or None

    def restore_and_foreground(self, hwnd: int, pids: set[int]) -> bool:
        del pids
        if self.user32.IsIconic(hwnd):
            self.user32.ShowWindow(hwnd, self.SW_RESTORE)
        deadline = time.monotonic() + 2.0
        while self.user32.IsIconic(hwnd) and time.monotonic() < deadline:
            time.sleep(0.05)
        if self.user32.IsIconic(hwnd):
            return False
        self.user32.BringWindowToTop(hwnd)
        self.user32.SetForegroundWindow(hwnd)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            foreground = self.user32.GetForegroundWindow()
            if foreground and int(foreground) == int(hwnd):
                return self._is_on_screen(hwnd)
            time.sleep(0.05)
        return False

    def _is_on_screen(self, hwnd: int) -> bool:
        left, top, right, bottom = self._rect(hwnd)
        vx = self.user32.GetSystemMetrics(self.SM_XVIRTUALSCREEN)
        vy = self.user32.GetSystemMetrics(self.SM_YVIRTUALSCREEN)
        vr = vx + self.user32.GetSystemMetrics(self.SM_CXVIRTUALSCREEN)
        vb = vy + self.user32.GetSystemMetrics(self.SM_CYVIRTUALSCREEN)
        return right > vx and bottom > vy and left < vr and top < vb

    def find_renderer(self, browser_hwnd: int) -> int | None:
        matches: list[tuple[int, int]] = []
        @_WNDENUMPROC
        def callback(hwnd: int, _lparam: int) -> bool:
            class_name = ctypes.create_unicode_buffer(256)
            self.user32.GetClassNameW(hwnd, class_name, len(class_name))
            if class_name.value == "Chrome_RenderWidgetHostHWND" and self.user32.IsWindowVisible(hwnd):
                try:
                    left, top, right, bottom = self._rect(hwnd, client=True)
                    matches.append((max(0, right - left) * max(0, bottom - top), int(hwnd)))
                except OSError:
                    pass
            return True

        self.user32.EnumChildWindows(browser_hwnd, callback, 0)
        return max(matches, default=(0, 0))[1] or None

    def renderer_metrics(self, hwnd: int) -> tuple[int, int, int, int]:
        left, top, right, bottom = self._rect(hwnd, client=True)
        point = wintypes.POINT(0, 0)
        if not self.user32.ClientToScreen(hwnd, ctypes.byref(point)):
            raise OSError("unable to map renderer coordinates")
        return int(point.x), int(point.y), right - left, bottom - top

    def _mouse_input(self, x: int, y: int, flags: int) -> Any:
        vx = self.user32.GetSystemMetrics(self.SM_XVIRTUALSCREEN)
        vy = self.user32.GetSystemMetrics(self.SM_YVIRTUALSCREEN)
        width = max(1, self.user32.GetSystemMetrics(self.SM_CXVIRTUALSCREEN) - 1)
        height = max(1, self.user32.GetSystemMetrics(self.SM_CYVIRTUALSCREEN) - 1)
        normalized_x = round((x - vx) * 65535 / width)
        normalized_y = round((y - vy) * 65535 / height)
        return _INPUT(
            type=self.INPUT_MOUSE,
            mi=_MOUSEINPUT(
                normalized_x,
                normalized_y,
                0,
                flags | self.MOUSEEVENTF_ABSOLUTE | self.MOUSEEVENTF_VIRTUALDESK,
                0,
                0,
            ),
        )

    def _send(self, event: Any) -> None:
        if self.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(_INPUT)) != 1:
            raise OSError("SendInput failed")

    def _keyboard_input(self, virtual_key: int, scan_code: int = 0, flags: int = 0) -> Any:
        return _INPUT(
            type=self.INPUT_KEYBOARD,
            ki=_KEYBDINPUT(virtual_key, scan_code, flags, 0, 0),
        )

    def press_shortcut(self, virtual_key: int) -> None:
        control_down = False
        key_down = False
        try:
            control_down = True
            self._send(self._keyboard_input(self.VK_CONTROL))
            key_down = True
            self._send(self._keyboard_input(virtual_key))
        finally:
            try:
                if key_down:
                    self._send(self._keyboard_input(virtual_key, flags=self.KEYEVENTF_KEYUP))
            finally:
                if control_down:
                    self._send(self._keyboard_input(self.VK_CONTROL, flags=self.KEYEVENTF_KEYUP))

    def press_key(self, virtual_key: int) -> None:
        key_down = False
        try:
            key_down = True
            self._send(self._keyboard_input(virtual_key))
        finally:
            if key_down:
                self._send(self._keyboard_input(virtual_key, flags=self.KEYEVENTF_KEYUP))

    def type_unicode(self, text: str) -> None:
        encoded = text.encode("utf-16-le")
        for offset in range(0, len(encoded), 2):
            scan_code = int.from_bytes(encoded[offset : offset + 2], "little")
            key_down = False
            try:
                key_down = True
                self._send(self._keyboard_input(0, scan_code, self.KEYEVENTF_UNICODE))
            finally:
                if key_down:
                    self._send(
                        self._keyboard_input(
                            0,
                            scan_code,
                            self.KEYEVENTF_UNICODE | self.KEYEVENTF_KEYUP,
                        )
                    )

    def drag_once(self, start: tuple[int, int], end: tuple[int, int]) -> None:
        down = False
        last_x, last_y = start
        try:
            self._send(self._mouse_input(*start, self.MOUSEEVENTF_MOVE))
            down = True
            self._send(self._mouse_input(*start, self.MOUSEEVENTF_LEFTDOWN))
            for index in range(1, 25):
                progress = index / 24
                eased = 0.5 - math.cos(math.pi * progress) / 2
                last_x = round(start[0] + (end[0] - start[0]) * eased)
                last_y = round(start[1] + math.sin(math.pi * progress) * 2)
                self._send(self._mouse_input(last_x, last_y, self.MOUSEEVENTF_MOVE))
                time.sleep(0.018 + (0.008 if index in {1, 23, 24} else 0))
        finally:
            if down:
                self._send(self._mouse_input(last_x, last_y, self.MOUSEEVENTF_LEFTUP))


def _positive_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _foreground_browser(native: Any, browser_process: Any) -> tuple[int, set[int]] | None:
    pid = getattr(browser_process, "pid", None)
    if not isinstance(pid, int) or pid <= 0:
        return None
    pids = native.process_tree(pid)
    browser_hwnd = native.find_browser_window(pids)
    if not browser_hwnd or not native.restore_and_foreground(browser_hwnd, pids):
        return None
    return browser_hwnd, pids


def open_native_tab(
    driver: Any,
    browser_process: Any,
    *,
    win32: Any | None = None,
    timeout: float = 3.0,
) -> str | None:
    """Open one Chromium tab through Ctrl+T and return its Selenium handle."""

    if not sys.platform.startswith("win") and win32 is None:
        return None
    native = win32 or _Win32()
    try:
        before = set(driver.window_handles)
        if _foreground_browser(native, browser_process) is None:
            return None
        native.press_shortcut(native.VK_T)
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            handles = list(driver.window_handles)
            created = [handle for handle in handles if handle not in before]
            if created:
                return str(created[-1])
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
    except Exception:
        return None
    return None


def navigate_native_url(browser_process: Any, url: str, *, win32: Any | None = None) -> bool:
    """Focus Chromium's address bar, enter a URL natively, and navigate."""

    if not sys.platform.startswith("win") and win32 is None:
        return False
    if not isinstance(url, str) or not url.strip():
        return False
    native = win32 or _Win32()
    try:
        if _foreground_browser(native, browser_process) is None:
            return False
        native.press_shortcut(native.VK_L)
        native.type_unicode(url)
        native.press_key(native.VK_RETURN)
    except Exception:
        return False
    return True


def attempt_native_slider(driver: Any, browser_process: Any, *, win32: Any | None = None) -> str:
    """Dispatch one native slider drag, returning a stable outcome string."""

    if not sys.platform.startswith("win") and win32 is None:
        return UNSUPPORTED
    try:
        present = driver.execute_script(_SLIDER_PRESENT_SCRIPT)
    except Exception:
        return NOT_READY
    if present is not True:
        return NOT_READY

    native = win32 or _Win32()
    try:
        focused = _foreground_browser(native, browser_process)
        if focused is None:
            return NOT_READY
        browser_hwnd, _pids = focused
        state = driver.execute_script(_SLIDER_STATE_SCRIPT)
        if not isinstance(state, dict) or not state.get("present"):
            return NOT_READY
        if state.get("failed"):
            return FAILED
        if state.get("completed") or not state.get("visible"):
            return NOT_READY
        wrapper = state.get("wrapper") or {}
        handle = state.get("handle") or {}
        viewport = state.get("viewport") or {}
        wrapper_width = _positive_number(wrapper.get("width"))
        handle_width = _positive_number(handle.get("width"))
        handle_height = _positive_number(handle.get("height"))
        viewport_width = _positive_number(viewport.get("width"))
        viewport_height = _positive_number(viewport.get("height"))
        try:
            wrapper_left = float(wrapper.get("left"))
            handle_left = float(handle.get("left"))
            handle_top = float(handle.get("top"))
        except (TypeError, ValueError):
            return NOT_READY
        if None in (wrapper_width, handle_width, handle_height, viewport_width, viewport_height):
            return NOT_READY
        travel = wrapper_width - handle_width
        if travel <= 0 or abs(handle_left - wrapper_left) > 3:
            return NOT_READY
        renderer = native.find_renderer(browser_hwnd)
        if not renderer:
            return NOT_READY
        origin_x, origin_y, client_width, client_height = native.renderer_metrics(renderer)
        if client_width <= 0 or client_height <= 0:
            return NOT_READY
        scale_x = client_width / viewport_width
        scale_y = client_height / viewport_height
        start = (
            round(origin_x + (handle_left + handle_width / 2) * scale_x),
            round(origin_y + (handle_top + handle_height / 2) * scale_y),
        )
        end = (round(start[0] + travel * scale_x), start[1])
        native.drag_once(start, end)
    except Exception:
        return FAILED
    return DISPATCHED
