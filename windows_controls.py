"""Windows global shortcuts and on-screen region selection."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import queue
import threading
import tkinter as tk

from recorder_core import Monitor


class GlobalHotkeys:
    SHORTCUTS = {1: ("start", ord("R")), 2: ("pause", ord("P")), 3: ("stop", ord("S"))}
    WM_HOTKEY = 0x0312
    WM_QUIT = 0x0012

    def __init__(self, events: queue.Queue):
        self.events = events
        self._ready = threading.Event()
        self._thread_id = 0
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.failures: list[str] = []
        self.labels: dict[str, str] = {}
        self._thread.start()
        self._ready.wait(timeout=3)

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        self._thread_id = kernel32.GetCurrentThreadId()
        registered: list[int] = []
        try:
            for hotkey_id, (action, key) in self.SHORTCUTS.items():
                combinations = [
                    (0x4000 | 0x0002 | 0x0001, "Ctrl+Alt"),
                    (0x4000 | 0x0002 | 0x0001 | 0x0004, "Ctrl+Alt+Shift"),
                ]
                for modifiers, label in combinations:
                    if user32.RegisterHotKey(None, hotkey_id, modifiers, key):
                        registered.append(hotkey_id)
                        self.labels[action] = f"{label}+{chr(key)}"
                        break
                else:
                    self.failures.append(action)
            self._ready.set()
            message = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == self.WM_HOTKEY and message.wParam in self.SHORTCUTS:
                    self.events.put(("hotkey", self.SHORTCUTS[message.wParam][0]))
        finally:
            for hotkey_id in registered:
                user32.UnregisterHotKey(None, hotkey_id)
            self._ready.set()

    def close(self) -> None:
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, self.WM_QUIT, 0, 0)
            self._thread.join(timeout=2)


class RegionSelector:
    def __init__(self, root: tk.Tk, monitor: Monitor, on_select):
        self.root = root
        self.monitor = monitor
        self.on_select = on_select
        self.start_x = 0
        self.start_y = 0
        self.rectangle = None

        top = tk.Toplevel(root)
        self.top = top
        top.overrideredirect(True)
        top.configure(bg="#111827")
        top.geometry(f"{monitor.width}x{monitor.height}+0+0")
        top.update_idletasks()
        user32 = ctypes.windll.user32
        user32.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.UINT,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL
        user32.SetWindowPos(
            top.winfo_id(), ctypes.c_void_p(-1),
            monitor.x, monitor.y, monitor.width, monitor.height, 0x0040,
        )
        top.attributes("-alpha", 0.45)
        canvas = tk.Canvas(top, bg="#111827", highlightthickness=0, cursor="crosshair")
        self.canvas = canvas
        canvas.pack(fill="both", expand=True)
        canvas.create_text(
            25, 25, anchor="nw", fill="white", font=("Segoe UI Semibold", 18),
            text="Kéo chuột để chọn vùng quay • Esc để hủy",
        )
        canvas.bind("<ButtonPress-1>", self._press)
        canvas.bind("<B1-Motion>", self._drag)
        canvas.bind("<ButtonRelease-1>", self._release)
        top.bind("<Escape>", lambda _event: self.close())
        top.focus_force()
        top.grab_set()

    def _press(self, event) -> None:
        self.start_x = event.x
        self.start_y = event.y
        if self.rectangle is not None:
            self.canvas.delete(self.rectangle)
        self.rectangle = self.canvas.create_rectangle(
            event.x, event.y, event.x, event.y,
            outline="#f87171", width=3, fill="#2563eb",
        )

    def _drag(self, event) -> None:
        if self.rectangle is not None:
            self.canvas.coords(self.rectangle, self.start_x, self.start_y, event.x, event.y)

    def _release(self, event) -> None:
        x1, x2 = sorted((self.start_x, event.x))
        y1, y2 = sorted((self.start_y, event.y))
        x1 = max(0, min(x1, self.monitor.width))
        x2 = max(0, min(x2, self.monitor.width))
        y1 = max(0, min(y1, self.monitor.height))
        y2 = max(0, min(y2, self.monitor.height))
        if x2 - x1 < 64 or y2 - y1 < 64:
            return
        region = Monitor("Vùng đã chọn", self.monitor.x + x1, self.monitor.y + y1, x2 - x1, y2 - y1)
        self.close()
        self.on_select(region)

    def close(self) -> None:
        if self.top.winfo_exists():
            self.top.grab_release()
            self.top.destroy()
