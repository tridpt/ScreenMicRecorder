"""Vietnamese desktop UI for screen and microphone recording."""

from __future__ import annotations

from datetime import timedelta
import os
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from recorder_core import (
    RecordingSettings,
    find_ffmpeg,
    list_capture_devices,
    list_monitors,
    set_dpi_awareness,
    unique_output_path,
)
from session import RecordingSession, list_system_audio_devices
from windows_controls import GlobalHotkeys, RegionSelector


BG = "#111827"
CARD = "#1f2937"
TEXT = "#f9fafb"
MUTED = "#a6b2c3"
ACCENT = "#ef4444"
BORDER = "#374151"


class ScreenMicApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Quay màn hình + Micro")
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.ffmpeg = None
        self.monitors = []
        self.microphones = []
        self.cameras = []
        self.system_audio_devices = []
        self.region = None
        self.session: RecordingSession | None = None
        self.state = "idle"
        self.busy = False
        self.elapsed_before_segment = 0.0
        self.segment_started = 0.0
        self.exit_after_stop = False
        self._events: queue.Queue = queue.Queue()

        self._style()
        self._layout()
        self._size_window()
        self._load_monitors()
        self.hotkeys = GlobalHotkeys(self._events)
        self.shortcuts_label.configure(
            text=" • ".join(
                f"{self.hotkeys.labels.get(action, 'Không có phím tắt')}: {name}"
                for action, name in (("start", "quay"), ("pause", "tạm dừng"), ("stop", "lưu"))
            )
        )
        self.root.after(100, self._drain_events)
        self.root.after(250, self._poll_recording)
        try:
            self.ffmpeg = find_ffmpeg()
        except FileNotFoundError as exc:
            self._set_status(str(exc), error=True)
            self.start_button.configure(state="disabled")
        else:
            self.root.after(100, self._refresh_devices)

    def _style(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            "Recorder.TCombobox",
            fieldbackground=CARD,
            background=CARD,
            foreground=TEXT,
            arrowcolor=TEXT,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            padding=8,
        )
        style.map(
            "Recorder.TCombobox",
            fieldbackground=[("readonly", CARD), ("disabled", CARD)],
            foreground=[("readonly", TEXT), ("disabled", MUTED)],
            selectbackground=[("readonly", CARD)],
            selectforeground=[("readonly", TEXT)],
        )
        style.configure("TProgressbar", troughcolor=CARD, background=ACCENT)
        self.root.option_add("*TCombobox*Listbox.background", CARD)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)

    def _layout(self) -> None:
        self.scroller = tk.Canvas(self.root, bg=BG, highlightthickness=0, bd=0)
        scrollbar = tk.Scrollbar(
            self.root, orient="vertical", command=self.scroller.yview,
            bg=BORDER, troughcolor=BG, activebackground=ACCENT,
            relief="flat", bd=0, highlightthickness=0,
        )
        self.scroller.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.scroller.pack(side="left", fill="both", expand=True)
        outer = tk.Frame(self.scroller, bg=BG, padx=25, pady=22)
        self.content = outer
        self.content_window = self.scroller.create_window((0, 0), window=outer, anchor="nw")
        outer.bind("<Configure>", lambda _event: self.scroller.configure(scrollregion=self.scroller.bbox("all")))
        self.scroller.bind(
            "<Configure>",
            lambda event: self.scroller.itemconfigure(self.content_window, width=event.width),
        )
        self.root.bind("<MouseWheel>", self._on_mousewheel)

        title_row = tk.Frame(outer, bg=BG)
        title_row.pack(fill="x")
        tk.Label(title_row, text="●", fg=ACCENT, bg=BG, font=("Segoe UI", 18)).pack(side="left", padx=(0, 9))
        tk.Label(
            title_row, text="Quay màn hình + Micro", fg=TEXT, bg=BG,
            font=("Segoe UI Semibold", 18),
        ).pack(side="left")
        tk.Label(
            outer, text="Quay màn hình, micro, tiếng máy và webcam thành video MP4.",
            fg=MUTED, bg=BG, font=("Segoe UI", 10), anchor="w",
        ).pack(fill="x", pady=(3, 17))

        card = tk.Frame(outer, bg=CARD, padx=18, pady=16)
        card.pack(fill="x")
        card.columnconfigure(0, weight=1)

        self._label(card, "Màn hình", 0, 0)
        self.monitor_combo = self._combo(card, 1, 0)
        self.monitor_combo.bind("<<ComboboxSelected>>", lambda _event: self._reset_region())

        region_row = tk.Frame(card, bg=CARD)
        region_row.grid(row=2, column=0, sticky="ew", pady=(0, 14))
        self.region_button = self._button(region_row, "Chọn vùng quay", self._select_region, BORDER)
        self.region_button.pack(side="left", ipady=5)
        self.clear_region_button = self._button(region_row, "Toàn màn hình", self._reset_region, BORDER)
        self.clear_region_button.pack(side="left", padx=(8, 0), ipady=5)
        self.region_label = tk.Label(
            region_row, text="Đang quay toàn màn hình", bg=CARD, fg=MUTED,
            font=("Segoe UI", 9), anchor="w",
        )
        self.region_label.pack(side="left", padx=(10, 0))

        self._label(card, "Micro", 3, 0)
        microphone_row = tk.Frame(card, bg=CARD)
        microphone_row.grid(row=4, column=0, sticky="ew", pady=(4, 14))
        microphone_row.columnconfigure(0, weight=1)
        self.microphone_combo = ttk.Combobox(
            microphone_row, state="readonly", style="Recorder.TCombobox"
        )
        self.microphone_combo.grid(row=0, column=0, sticky="ew")
        self.refresh_button = self._button(
            microphone_row, "Làm mới", self._refresh_devices, background=BORDER
        )
        self.refresh_button.grid(row=0, column=1, padx=(9, 0), ipady=5)

        self._label(card, "Tiếng máy tính (loa/tai nghe)", 5, 0)
        self.system_combo = self._combo(card, 6, 0)
        self.system_combo.configure(values=["Không thu tiếng máy"])
        self.system_combo.current(0)

        self._label(card, "Webcam trong video", 7, 0)
        camera_row = tk.Frame(card, bg=CARD)
        camera_row.grid(row=8, column=0, sticky="ew", pady=(4, 14))
        camera_row.columnconfigure(0, weight=2)
        camera_row.columnconfigure(1, weight=1)
        self.camera_combo = ttk.Combobox(camera_row, state="readonly", style="Recorder.TCombobox")
        self.camera_combo.configure(values=["Không dùng webcam"])
        self.camera_combo.current(0)
        self.camera_combo.grid(row=0, column=0, sticky="ew", padx=(0, 7))
        self.corner_combo = ttk.Combobox(
            camera_row, values=["Dưới phải", "Dưới trái", "Trên phải", "Trên trái"],
            state="readonly", style="Recorder.TCombobox",
        )
        self.corner_combo.set("Dưới phải")
        self.corner_combo.grid(row=0, column=1, sticky="ew", padx=(7, 0))

        options = tk.Frame(card, bg=CARD)
        options.grid(row=9, column=0, sticky="ew")
        options.columnconfigure(0, weight=1)
        options.columnconfigure(1, weight=1)
        self._label(options, "Khung hình/giây", 0, 0)
        self._label(options, "Độ phân giải", 0, 1)
        self.fps_combo = ttk.Combobox(
            options, values=["15", "20", "30"], state="readonly", style="Recorder.TCombobox"
        )
        self.fps_combo.set("15")
        self.fps_combo.grid(row=1, column=0, sticky="ew", padx=(0, 7), pady=(4, 14))
        self.resolution_combo = ttk.Combobox(
            options, values=["1080p", "720p", "Gốc"], state="readonly",
            style="Recorder.TCombobox",
        )
        self.resolution_combo.set("720p")
        self.resolution_combo.grid(row=1, column=1, sticky="ew", padx=(7, 0), pady=(4, 14))

        self._label(card, "Thư mục lưu video", 10, 0)
        folder_row = tk.Frame(card, bg=CARD)
        folder_row.grid(row=11, column=0, sticky="ew", pady=(4, 0))
        folder_row.columnconfigure(0, weight=1)
        self.folder_var = tk.StringVar(value=str(Path.home() / "Videos" / "ScreenMicRecorder"))
        self.folder_entry = tk.Entry(
            folder_row, textvariable=self.folder_var, bg=BG, fg=TEXT,
            insertbackground=TEXT, relief="flat", highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT,
            font=("Segoe UI", 10),
        )
        self.folder_entry.grid(row=0, column=0, sticky="ew", ipady=8)
        self.browse_button = self._button(
            folder_row, "Chọn...", self._browse_folder, background=BORDER
        )
        self.browse_button.grid(row=0, column=1, padx=(9, 0), ipady=5)

        action_row = tk.Frame(outer, bg=BG)
        action_row.pack(fill="x", pady=(21, 0))
        self.start_button = self._button(
            action_row, "●  Bắt đầu quay", self._start_recording,
            background=ACCENT, font=("Segoe UI Semibold", 12),
        )
        self.start_button.pack(side="left", fill="x", expand=True, ipady=11)
        self.pause_button = self._button(
            action_row, "Ⅱ  Tạm dừng", self._toggle_pause,
            background=BORDER, font=("Segoe UI Semibold", 12),
        )
        self.pause_button.configure(state="disabled")
        self.pause_button.pack(side="left", fill="x", expand=True, padx=(10, 0), ipady=11)
        self.stop_button = self._button(
            action_row, "■  Dừng & lưu", self._stop_recording,
            background=BORDER, font=("Segoe UI Semibold", 12),
        )
        self.stop_button.configure(state="disabled")
        self.stop_button.pack(side="left", fill="x", expand=True, padx=(10, 0), ipady=11)

        footer = tk.Frame(outer, bg=BG)
        footer.pack(fill="x", pady=(16, 0))
        self.status_label = tk.Label(
            footer, text="Sẵn sàng", fg=MUTED, bg=BG,
            font=("Segoe UI", 10), anchor="w", justify="left", wraplength=490,
        )
        self.status_label.pack(side="left", fill="x", expand=True)
        self.timer_label = tk.Label(
            footer, text="00:00:00", fg=TEXT, bg=BG,
            font=("Consolas", 14), anchor="e",
        )
        self.timer_label.pack(side="right")

        bottom = tk.Frame(outer, bg=BG)
        bottom.pack(fill="x", pady=(12, 0))
        self.open_button = self._button(
            bottom, "Mở thư mục video", self._open_folder, background=BG,
            foreground="#93c5fd", font=("Segoe UI", 10),
        )
        self.open_button.pack(anchor="w")
        self.shortcuts_label = tk.Label(
            bottom, text="",
            bg=BG, fg=MUTED, font=("Segoe UI", 9),
            anchor="w", justify="left", wraplength=750,
        )
        self.shortcuts_label.pack(fill="x", pady=(7, 0))

    def _size_window(self) -> None:
        self.root.update_idletasks()
        width = min(self.content.winfo_reqwidth() + 20, self.root.winfo_screenwidth() - 60)
        height = min(self.content.winfo_reqheight() + 20, self.root.winfo_screenheight() - 100)
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(min(560, width), min(470, height))

    def _on_mousewheel(self, event) -> None:
        self.scroller.yview_scroll(-int(event.delta / 120), "units")

    def _label(self, parent: tk.Widget, text: str, row: int, column: int) -> None:
        tk.Label(
            parent, text=text, bg=CARD, fg=TEXT, font=("Segoe UI Semibold", 10),
            anchor="w",
        ).grid(row=row, column=column, sticky="w")

    def _combo(self, parent: tk.Widget, row: int, column: int) -> ttk.Combobox:
        combo = ttk.Combobox(parent, state="readonly", style="Recorder.TCombobox")
        combo.grid(row=row, column=column, sticky="ew", pady=(4, 14))
        return combo

    def _button(self, parent: tk.Widget, text: str, command, background: str,
                foreground: str = TEXT, font=("Segoe UI", 10)) -> tk.Button:
        return tk.Button(
            parent, text=text, command=command, bg=background, fg=foreground,
            activebackground=background, activeforeground=foreground,
            disabledforeground=MUTED, relief="flat", bd=0, padx=13,
            cursor="hand2", font=font,
        )

    def _set_status(self, message: str, error: bool = False) -> None:
        self.status_label.configure(text=message, fg="#fca5a5" if error else MUTED)

    def _drain_events(self) -> None:
        while True:
            try:
                event, payload = self._events.get_nowait()
            except queue.Empty:
                break
            if event == "devices":
                self._devices_loaded(*payload)
            elif event == "started":
                self._started(*payload)
            elif event == "paused":
                self._pause_changed(*payload)
            elif event == "saved":
                self._saved(*payload)
            elif event == "hotkey":
                self._handle_hotkey(payload)
        if self.root.winfo_exists():
            self.root.after(100, self._drain_events)

    def _load_monitors(self) -> None:
        try:
            self.monitors = list_monitors()
        except RuntimeError as exc:
            self._set_status(str(exc), error=True)
            self.start_button.configure(state="disabled")
            return
        self.monitor_combo.configure(values=[monitor.label for monitor in self.monitors])
        self.monitor_combo.current(0)

    def _select_region(self) -> None:
        index = self.monitor_combo.current()
        if index >= 0 and self.state == "idle":
            RegionSelector(self.root, self.monitors[index], self._region_selected)

    def _region_selected(self, region) -> None:
        self.region = region
        self.region_label.configure(text=f"{region.width} × {region.height} tại ({region.x}, {region.y})")

    def _reset_region(self) -> None:
        self.region = None
        self.region_label.configure(text="Đang quay toàn màn hình")

    def _refresh_devices(self) -> None:
        if self.ffmpeg is None or self.state != "idle":
            return
        self.refresh_button.configure(state="disabled")
        self.microphone_combo.configure(values=[])
        self.microphone_combo.set("Đang tìm thiết bị...")
        self.start_button.configure(state="disabled")

        def work():
            try:
                microphones, cameras = list_capture_devices(self.ffmpeg)
                error = None
            except (OSError, RuntimeError, ValueError) as exc:
                microphones, cameras, error = [], [], str(exc)
            try:
                system_devices = list_system_audio_devices()
            except (OSError, RuntimeError, ValueError):
                system_devices = []
            self._events.put(("devices", (microphones, cameras, system_devices, error)))

        threading.Thread(target=work, daemon=True).start()

    def _devices_loaded(self, microphones, cameras, system_devices, error) -> None:
        self.microphones = microphones
        self.cameras = cameras
        self.system_audio_devices = system_devices
        self.refresh_button.configure(state="normal")
        self.microphone_combo.configure(values=[mic.name for mic in microphones])
        self.system_combo.configure(values=["Không thu tiếng máy"] + [device.name for device in system_devices])
        self.system_combo.current(1 if system_devices else 0)
        self.camera_combo.configure(values=["Không dùng webcam"] + [camera.name for camera in cameras])
        self.camera_combo.current(0)
        if microphones:
            self.microphone_combo.current(0)
            self.start_button.configure(state="normal" if self.monitors else "disabled")
            self._set_status("Sẵn sàng. Chọn vùng, tiếng máy và webcam theo ý muốn.")
        else:
            self.microphone_combo.set("Không tìm thấy micro")
            self._set_status(error or "Không tìm thấy micro. Kiểm tra kết nối và quyền truy cập micro.", True)

    def _browse_folder(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.folder_var.get() or str(Path.home()))
        if chosen:
            self.folder_var.set(chosen)

    def _open_folder(self) -> None:
        folder = Path(self.folder_var.get().strip()).expanduser()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(folder)
        except OSError as exc:
            messagebox.showerror("Không mở được thư mục", str(exc), parent=self.root)

    def _start_recording(self) -> None:
        if self.state != "idle" or self.busy or self.ffmpeg is None:
            return
        monitor_index = self.monitor_combo.current()
        mic_index = self.microphone_combo.current()
        if monitor_index < 0 or mic_index < 0:
            messagebox.showerror("Thiếu thiết bị", "Hãy chọn màn hình và micro.", parent=self.root)
            return
        folder_text = self.folder_var.get().strip()
        if not folder_text:
            messagebox.showerror("Thiếu thư mục", "Hãy chọn thư mục lưu video.", parent=self.root)
            return
        try:
            output = unique_output_path(Path(folder_text).expanduser())
            settings = RecordingSettings(
                monitor=self.region or self.monitors[monitor_index],
                microphone=self.microphones[mic_index],
                fps=int(self.fps_combo.get()),
                resolution=self.resolution_combo.get(),
                output=output,
                camera=self.cameras[self.camera_combo.current() - 1] if self.camera_combo.current() > 0 else None,
                camera_corner=self.corner_combo.get(),
            )
            system_audio = (
                self.system_audio_devices[self.system_combo.current() - 1]
                if self.system_combo.current() > 0 else None
            )
        except (OSError, ValueError, RuntimeError) as exc:
            messagebox.showerror("Không bắt đầu được", str(exc), parent=self.root)
            return
        self.busy = True
        self.state = "starting"
        self.elapsed_before_segment = 0.0
        self.timer_label.configure(text="00:00:00")
        self._set_inputs_enabled(False)
        self._update_buttons()
        self._set_status("Đang chuẩn bị thiết bị quay...")

        def work():
            try:
                session = RecordingSession(self.ffmpeg, settings, system_audio)
                session.start()
                error = None
            except Exception as exc:
                session, error = None, str(exc)
            self._events.put(("started", (session, error)))

        threading.Thread(target=work, daemon=True).start()

    def _started(self, session, error) -> None:
        self.busy = False
        if error:
            self.state = "idle"
            self._set_inputs_enabled(True)
            self._update_buttons()
            self._set_status("Không bắt đầu được phiên quay.", True)
            messagebox.showerror("Không bắt đầu được", error, parent=self.root)
            if self.exit_after_stop:
                self._destroy()
            return
        self.session = session
        self.state = "recording"
        self.segment_started = time.monotonic()
        self._update_buttons()
        self._set_status(f"Đang quay: {session.settings.output.name}")
        if self.exit_after_stop:
            self._stop_recording()

    def _poll_recording(self) -> None:
        if self.state == "recording":
            elapsed = self.elapsed_before_segment + time.monotonic() - self.segment_started
            self.timer_label.configure(text=str(timedelta(seconds=int(elapsed))))
            if not self.busy and self.session and self.session.process and self.session.process.poll() is not None:
                self._set_status("Thiết bị quay đã dừng; đang lưu phần video đã ghi.", True)
                self._stop_recording()
        self.root.after(250, self._poll_recording)

    def _toggle_pause(self) -> None:
        if self.busy or self.session is None or self.state not in ("recording", "paused"):
            return
        was_recording = self.state == "recording"
        self.busy = True
        self._update_buttons()
        self._set_status("Đang tạm dừng..." if was_recording else "Đang tiếp tục quay...")
        session = self.session

        def work():
            try:
                if was_recording:
                    session.pause()
                else:
                    session.resume()
                error = None
            except Exception as exc:
                error = str(exc)
            self._events.put(("paused", (was_recording, error)))

        threading.Thread(target=work, daemon=True).start()

    def _pause_changed(self, was_recording: bool, error: str | None) -> None:
        self.busy = False
        if error:
            self.state = "paused" if self.session and self.session.state == "paused" else "recording"
            self._set_status("Không chuyển trạng thái quay được.", True)
            messagebox.showerror("Lỗi tạm dừng", error, parent=self.root)
        elif was_recording:
            self.elapsed_before_segment += time.monotonic() - self.segment_started
            self.state = "paused"
            self._set_status("Đã tạm dừng. Nhấn Tiếp tục hoặc Ctrl+Alt+P.")
        else:
            self.segment_started = time.monotonic()
            self.state = "recording"
            self._set_status("Đang tiếp tục quay.")
        self._update_buttons()
        if self.exit_after_stop:
            self._stop_recording()

    def _stop_recording(self) -> None:
        if self.busy or self.session is None or self.state not in ("recording", "paused"):
            return
        self.busy = True
        self.state = "saving"
        self._update_buttons()
        self._set_status("Đang ghép các đoạn và lưu video...")
        session = self.session

        def work():
            try:
                result = session.stop()
                error = None
            except Exception as exc:
                result, error = None, str(exc)
            self._events.put(("saved", (result, error)))

        threading.Thread(target=work, daemon=True).start()

    def _saved(self, result, error) -> None:
        self.busy = False
        self.state = "idle"
        self.session = None
        self._set_inputs_enabled(True)
        self._update_buttons()
        if result and result.ok:
            self._set_status(f"Đã lưu: {result.output.name}")
        else:
            self._set_status("Lưu video thất bại. Xem thông báo lỗi.", True)
            messagebox.showerror("Lỗi lưu video", error or "Không tạo được tệp MP4.", parent=self.root)
        if self.exit_after_stop:
            self._destroy()

    def _set_inputs_enabled(self, enabled: bool) -> None:
        combo_state = "readonly" if enabled else "disabled"
        for combo in (
            self.monitor_combo, self.microphone_combo, self.system_combo,
            self.camera_combo, self.corner_combo, self.fps_combo, self.resolution_combo,
        ):
            combo.configure(state=combo_state)
        for button in (self.region_button, self.clear_region_button, self.refresh_button, self.browse_button):
            button.configure(state="normal" if enabled else "disabled")
        self.folder_entry.configure(state="normal" if enabled else "disabled")

    def _update_buttons(self) -> None:
        self.start_button.configure(
            state="normal" if self.state == "idle" and not self.busy and self.microphones and self.ffmpeg else "disabled"
        )
        self.pause_button.configure(
            state="normal" if self.state in ("recording", "paused") and not self.busy else "disabled",
            text="▶  Tiếp tục" if self.state == "paused" else "Ⅱ  Tạm dừng",
        )
        self.stop_button.configure(
            state="normal" if self.state in ("recording", "paused") and not self.busy else "disabled"
        )

    def _handle_hotkey(self, action: str) -> None:
        if action == "start":
            self._start_recording()
        elif action == "pause":
            self._toggle_pause()
        elif action == "stop":
            self._stop_recording()

    def _on_close(self) -> None:
        if self.state == "idle" and not self.busy:
            self._destroy()
            return
        self.exit_after_stop = True
        if not self.busy:
            self._stop_recording()

    def _destroy(self) -> None:
        self.hotkeys.close()
        self.root.destroy()


if __name__ == "__main__":
    set_dpi_awareness()
    root = tk.Tk()
    ScreenMicApp(root)
    root.mainloop()
