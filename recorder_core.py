"""Windows screen and microphone capture backed by FFmpeg."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading


CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass(frozen=True)
class Monitor:
    name: str
    x: int
    y: int
    width: int
    height: int
    primary: bool = False

    @property
    def label(self) -> str:
        suffix = " • Chính" if self.primary else ""
        return f"{self.name} ({self.width} × {self.height}){suffix}"


@dataclass(frozen=True)
class Microphone:
    name: str
    device: str


@dataclass(frozen=True)
class Camera:
    name: str
    device: str


@dataclass(frozen=True)
class RecordingSettings:
    monitor: Monitor
    microphone: Microphone
    fps: int
    resolution: str
    output: Path
    camera: Camera | None = None
    camera_corner: str = "Dưới phải"


@dataclass(frozen=True)
class RecordingResult:
    returncode: int
    output: Path
    size_bytes: int
    log_tail: str
    forced_stop: bool

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and self.size_bytes > 0 and not self.forced_stop


def set_dpi_awareness() -> None:
    """Keep monitor coordinates and FFmpeg pixels in the same coordinate space."""
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        ctypes.windll.user32.SetProcessDPIAware()


def list_monitors() -> list[Monitor]:
    if sys.platform != "win32":
        raise RuntimeError("Ứng dụng chỉ hỗ trợ Windows.")

    class MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
        ]

    user32 = ctypes.windll.user32
    user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        wintypes.LPARAM,
    )
    found: list[Monitor] = []

    def callback(handle, _dc, _rect, _data):
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            rect = info.rcMonitor
            found.append(
                Monitor(
                    name="Màn hình",
                    x=rect.left,
                    y=rect.top,
                    width=rect.right - rect.left,
                    height=rect.bottom - rect.top,
                    primary=bool(info.dwFlags & 1),
                )
            )
        return True

    callback_ref = callback_type(callback)
    if not user32.EnumDisplayMonitors(None, None, callback_ref, 0) or not found:
        raise RuntimeError("Không tìm thấy màn hình để quay.")

    found.sort(key=lambda m: (not m.primary, m.x, m.y))
    named = [
        Monitor(f"Màn hình {number}", m.x, m.y, m.width, m.height, m.primary)
        for number, m in enumerate(found, 1)
    ]
    if len(named) == 1:
        return named

    left = min(m.x for m in named)
    top = min(m.y for m in named)
    right = max(m.x + m.width for m in named)
    bottom = max(m.y + m.height for m in named)
    all_screens = Monitor("Tất cả màn hình", left, top, right - left, bottom - top)
    return named + [all_screens]


def find_ffmpeg() -> Path:
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys._MEIPASS) / "ffmpeg.exe")
        candidates.append(Path(sys._MEIPASS) / "win32" / "ffmpeg.exe")
    candidates.append(Path(__file__).resolve().parent / "bin" / "ffmpeg.exe")
    on_path = shutil.which("ffmpeg")
    if on_path:
        candidates.append(Path(on_path))
    try:
        import static_ffmpeg

        candidates.append(Path(static_ffmpeg.__file__).parent / "bin" / "win32" / "ffmpeg.exe")
    except ImportError:
        pass
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("Không tìm thấy FFmpeg. Hãy dùng bản ứng dụng đã đóng gói.")


def parse_devices(ffmpeg_output: str, kind: str) -> list[tuple[str, str]]:
    devices: list[list[str]] = []
    current_device: int | None = None
    for line in ffmpeg_output.splitlines():
        match = re.search(r'"(.+)" \((audio|video)\)\s*$', line)
        if match:
            if match.group(2) == kind:
                devices.append([match.group(1), match.group(1)])
                current_device = len(devices) - 1
            else:
                current_device = None
            continue
        alternative = re.search(r'Alternative name "(.+)"\s*$', line)
        if alternative and current_device is not None:
            devices[current_device][1] = alternative.group(1)
            current_device = None
    return [(name, device) for name, device in devices]


def parse_microphones(ffmpeg_output: str) -> list[Microphone]:
    return [Microphone(*device) for device in parse_devices(ffmpeg_output, "audio")]


def parse_cameras(ffmpeg_output: str) -> list[Camera]:
    return [Camera(*device) for device in parse_devices(ffmpeg_output, "video")]


def decode_ffmpeg_output(data: bytes) -> str:
    decoded = data.decode("utf-8", errors="replace")
    if "\ufffd" in decoded and sys.platform == "win32":
        return data.decode("mbcs", errors="replace")
    return decoded


def list_capture_devices(ffmpeg: Path) -> tuple[list[Microphone], list[Camera]]:
    command = [str(ffmpeg), "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=15,
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Tìm micro quá thời gian. Hãy thử lại.") from exc
    output = decode_ffmpeg_output(result.stderr or result.stdout)
    return parse_microphones(output), parse_cameras(output)


def list_microphones(ffmpeg: Path) -> list[Microphone]:
    return list_capture_devices(ffmpeg)[0]


def unique_output_path(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    stem = datetime.now().strftime("Quay_man_hinh_%Y-%m-%d_%H-%M-%S")
    output = folder / f"{stem}.mp4"
    number = 2
    while output.exists():
        output = folder / f"{stem}_{number}.mp4"
        number += 1
    return output


def build_command(ffmpeg: Path, settings: RecordingSettings) -> list[str]:
    monitor = settings.monitor
    if settings.fps not in (15, 20, 30):
        raise ValueError("Tốc độ khung hình không hợp lệ.")
    if monitor.width <= 0 or monitor.height <= 0:
        raise ValueError("Kích thước màn hình không hợp lệ.")

    filters = {
        "Gốc": "pad=ceil(iw/2)*2:ceil(ih/2)*2",
        "1080p": "scale=w='min(1920,iw)':h='min(1080,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2:flags=fast_bilinear",
        "720p": "scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2:flags=fast_bilinear",
    }
    if settings.resolution not in filters:
        raise ValueError("Độ phân giải không hợp lệ.")
    command = [
        str(ffmpeg),
        "-hide_banner", "-loglevel", "warning", "-nostats", "-y",
        "-thread_queue_size", "512",
        "-f", "gdigrab", "-framerate", str(settings.fps),
        "-draw_mouse", "1",
        "-offset_x", str(monitor.x), "-offset_y", str(monitor.y),
        "-video_size", f"{monitor.width}x{monitor.height}", "-i", "desktop",
        "-thread_queue_size", "512", "-rtbufsize", "256M",
        "-f", "dshow", "-i", f"audio={settings.microphone.device}",
    ]
    command += ["-vf", filters[settings.resolution], "-map", "0:v:0"]
    command += [
        "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "25",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
        "-af", "aresample=async=1:first_pts=0",
        "-movflags", "+faststart", str(settings.output),
    ]
    return command


class Recorder:
    def __init__(self, ffmpeg: Path, settings: RecordingSettings):
        self.ffmpeg = ffmpeg
        self.settings = settings
        self.process: subprocess.Popen | None = None
        self._log = deque(maxlen=80)
        self._reader: threading.Thread | None = None

    def start(self) -> None:
        if self.process is not None:
            raise RuntimeError("Phiên quay đã bắt đầu.")
        self.settings.output.parent.mkdir(parents=True, exist_ok=True)
        self.process = subprocess.Popen(
            build_command(self.ffmpeg, self.settings),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )
        self._reader = threading.Thread(target=self._read_log, daemon=True)
        self._reader.start()

    def _read_log(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        with self.process.stderr:
            for line in self.process.stderr:
                decoded = decode_ffmpeg_output(line).strip()
                if decoded:
                    self._log.append(decoded)

    def stop(self) -> RecordingResult:
        if self.process is None:
            raise RuntimeError("Chưa có phiên quay.")
        process = self.process
        forced = False
        if process.poll() is None and process.stdin is not None:
            try:
                process.stdin.write(b"q\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
        try:
            returncode = process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            forced = True
            process.terminate()
            try:
                returncode = process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                returncode = process.wait(timeout=3)
        if process.stdin:
            process.stdin.close()
        if self._reader:
            self._reader.join(timeout=2)
        size = self.settings.output.stat().st_size if self.settings.output.exists() else 0
        return RecordingResult(
            returncode=returncode,
            output=self.settings.output,
            size_bytes=size,
            log_tail="\n".join(list(self._log)[-8:]),
            forced_stop=forced,
        )
