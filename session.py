"""Pauseable recording sessions and WASAPI loopback capture."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import wave

from recorder_core import (
    CREATE_NO_WINDOW,
    Recorder,
    RecordingResult,
    RecordingSettings,
    decode_ffmpeg_output,
)


@dataclass(frozen=True)
class SystemAudioDevice:
    index: int
    name: str
    channels: int
    sample_rate: int


def list_system_audio_devices() -> list[SystemAudioDevice]:
    try:
        import pyaudiowpatch as pyaudio
    except ImportError:
        return []
    audio = pyaudio.PyAudio()
    try:
        return [
            SystemAudioDevice(
                index=int(info["index"]),
                name=str(info["name"]).removesuffix(" [Loopback]"),
                channels=min(2, int(info["maxInputChannels"])),
                sample_rate=int(info["defaultSampleRate"]),
            )
            for info in audio.get_loopback_device_info_generator()
            if int(info["maxInputChannels"]) > 0
        ]
    finally:
        audio.terminate()


class LoopbackCapture:
    def __init__(self, device: SystemAudioDevice, output: Path):
        self.device = device
        self.output = output
        self._audio = None
        self._stream = None
        self._wave = None
        self._writer: threading.Thread | None = None
        self._queue: queue.Queue = queue.Queue(maxsize=2000)
        self._origin = 0.0
        self._error: Exception | None = None
        self._lost_chunks = 0

    def start(self, origin: float) -> None:
        import pyaudiowpatch as pyaudio

        self._origin = origin
        self._wave = wave.open(str(self.output), "wb")
        self._wave.setnchannels(self.device.channels)
        self._wave.setsampwidth(2)
        self._wave.setframerate(self.device.sample_rate)
        self._writer = threading.Thread(target=self._write_audio, daemon=True)
        self._writer.start()
        try:
            self._audio = pyaudio.PyAudio()
            self._stream = self._audio.open(
                format=pyaudio.paInt16,
                channels=self.device.channels,
                rate=self.device.sample_rate,
                input=True,
                input_device_index=self.device.index,
                frames_per_buffer=1024,
                stream_callback=self._callback,
            )
        except Exception:
            self.stop()
            raise

    def _callback(self, data, frame_count, _time_info, _status):
        import pyaudiowpatch as pyaudio

        try:
            self._queue.put_nowait((time.monotonic(), data, frame_count))
        except queue.Full:
            self._lost_chunks += 1
        return (None, pyaudio.paContinue)

    def _write_audio(self) -> None:
        assert self._wave is not None
        rate = self.device.sample_rate
        frame_size = self.device.channels * 2
        written = 0
        try:
            while True:
                item = self._queue.get()
                if item[0] == "stop":
                    stop_time = item[1]
                    break
                timestamp, data, frame_count = item
                actual_frames = min(frame_count, len(data) // frame_size)
                start_frame = max(0, round((timestamp - self._origin) * rate) - actual_frames)
                if start_frame > written:
                    gap = start_frame - written
                    self._wave.writeframesraw(b"\0" * (gap * frame_size))
                    written += gap
                skip = max(0, written - start_frame)
                if skip < actual_frames:
                    payload = data[skip * frame_size:actual_frames * frame_size]
                    self._wave.writeframesraw(payload)
                    written += len(payload) // frame_size
            target_frames = max(written, round((stop_time - self._origin) * rate))
            if target_frames > written:
                self._wave.writeframesraw(b"\0" * ((target_frames - written) * frame_size))
        except Exception as exc:
            self._error = exc
        finally:
            self._wave.close()

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop_stream()
            finally:
                self._stream.close()
                self._stream = None
        if self._audio is not None:
            self._audio.terminate()
            self._audio = None
        if self._writer is not None:
            self._queue.put(("stop", time.monotonic()))
            self._writer.join(timeout=10)
            if self._writer.is_alive():
                raise RuntimeError("Không kết thúc được luồng ghi tiếng máy.")
            self._writer = None
        if self._error is not None:
            raise RuntimeError(f"Lỗi ghi tiếng máy: {self._error}") from self._error


class CameraCapture:
    def __init__(self, ffmpeg: Path, device: str, output: Path):
        self.ffmpeg = ffmpeg
        self.device = device
        self.output = output
        self.process: subprocess.Popen | None = None

    def start(self) -> None:
        self.process = subprocess.Popen(
            [
                str(self.ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
                "-f", "dshow", "-i", f"video={self.device}",
                "-an", "-c:v", "copy",
                "-f", "matroska", str(self.output),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )

    def stop(self) -> None:
        if self.process is None:
            return
        process = self.process
        if process.poll() is None and process.stdin:
            try:
                process.stdin.write(b"q\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
        try:
            _, stderr = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                _, stderr = process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                _, stderr = process.communicate(timeout=3)
            raise RuntimeError("Webcam không dừng đúng cách.")
        if process.returncode != 0 or not self.output.exists() or self.output.stat().st_size == 0:
            raise RuntimeError(f"Không ghi được webcam.\n{decode_ffmpeg_output(stderr)[-2000:]}")


class RecordingSession:
    def __init__(
        self,
        ffmpeg: Path,
        settings: RecordingSettings,
        system_audio: SystemAudioDevice | None,
    ):
        self.ffmpeg = ffmpeg
        self.settings = settings
        self.system_audio = system_audio
        self.state = "idle"
        self._directory: Path | None = None
        self._segments: list[tuple[Path, Path | None, Path | None]] = []
        self._recorder: Recorder | None = None
        self._loopback: LoopbackCapture | None = None
        self._camera: CameraCapture | None = None

    @property
    def process(self):
        return self._recorder.process if self._recorder else None

    def start(self) -> None:
        if self.state != "idle":
            raise RuntimeError("Phiên quay đã bắt đầu.")
        self.settings.output.parent.mkdir(parents=True, exist_ok=True)
        self._directory = Path(tempfile.mkdtemp(
            prefix=f".{self.settings.output.stem}_parts_",
            dir=self.settings.output.parent,
        ))
        try:
            self._start_segment()
        except Exception:
            shutil.rmtree(self._directory, ignore_errors=True)
            self._directory = None
            raise
        self.state = "recording"

    def _start_segment(self) -> None:
        assert self._directory is not None
        number = len(self._segments)
        video = self._directory / f"part-{number:03}.mp4"
        audio = self._directory / f"part-{number:03}.wav" if self.system_audio else None
        camera_file = self._directory / f"camera-{number:03}.mkv" if self.settings.camera else None
        camera_capture = None
        if self.settings.camera is not None and camera_file is not None:
            camera_capture = CameraCapture(self.ffmpeg, self.settings.camera.device, camera_file)
            camera_capture.start()
        recorder = Recorder(self.ffmpeg, replace(self.settings, output=video))
        origin = time.monotonic()
        loopback = None
        try:
            recorder.start()
            if self.system_audio is not None and audio is not None:
                loopback = LoopbackCapture(self.system_audio, audio)
                loopback.start(origin)
        except Exception:
            if recorder.process:
                recorder.stop()
            if camera_capture:
                camera_capture.stop()
            raise
        self._recorder = recorder
        self._loopback = loopback
        self._camera = camera_capture
        self._segments.append((video, audio, camera_file))

    def _end_segment(self) -> None:
        if self._recorder is None:
            return
        recorder = self._recorder
        loopback = self._loopback
        camera = self._camera
        self._recorder = None
        self._loopback = None
        self._camera = None
        try:
            result = recorder.stop()
        finally:
            try:
                if camera:
                    camera.stop()
            finally:
                if loopback:
                    loopback.stop()
        if not result.ok:
            raise RuntimeError(f"Không hoàn tất đoạn quay.\n{result.log_tail}")

    def pause(self) -> None:
        if self.state != "recording":
            raise RuntimeError("Không có đoạn quay để tạm dừng.")
        self._end_segment()
        self.state = "paused"

    def resume(self) -> None:
        if self.state != "paused":
            raise RuntimeError("Phiên quay chưa tạm dừng.")
        self._start_segment()
        self.state = "recording"

    def stop(self) -> RecordingResult:
        if self.state not in ("recording", "paused"):
            raise RuntimeError("Chưa có phiên quay để lưu.")
        self.state = "saving"
        try:
            self._end_segment()
            self._finalize()
            size = self.settings.output.stat().st_size
            result = RecordingResult(0, self.settings.output, size, "", False)
            assert self._directory is not None
            shutil.rmtree(self._directory)
            self._directory = None
            self.state = "finished"
            return result
        except Exception as exc:
            self.state = "failed"
            recovery = f"\nCác đoạn gốc còn ở: {self._directory}" if self._directory else ""
            raise RuntimeError(f"Không lưu được video: {exc}{recovery}") from exc

    def _finalize(self) -> None:
        assert self._directory is not None
        ready: list[Path] = []
        for index, (video, audio, camera) in enumerate(self._segments):
            if camera is not None:
                with_camera = self._directory / f"overlay-{index:03}.mp4"
                corners = {
                    "Trên trái": ("16", "16"),
                    "Trên phải": ("main_w-overlay_w-16", "16"),
                    "Dưới trái": ("16", "main_h-overlay_h-16"),
                    "Dưới phải": ("main_w-overlay_w-16", "main_h-overlay_h-16"),
                }
                if self.settings.camera_corner not in corners:
                    raise ValueError("Vị trí webcam không hợp lệ.")
                x, y = corners[self.settings.camera_corner]
                monitor = self.settings.monitor
                max_size = {"1080p": (1920, 1080), "720p": (1280, 720), "Gốc": (monitor.width, monitor.height)}
                max_width, max_height = max_size[self.settings.resolution]
                scale = min(1.0, max_width / monitor.width, max_height / monitor.height)
                width = max(2, int(monitor.width * scale) // 2 * 2)
                height = max(2, int(monitor.height * scale) // 2 * 2)
                camera_width = max(2, min(320, width // 4, height // 3) // 2 * 2)
                overlay = (
                    f"[0:v]setpts=PTS-STARTPTS[screen];"
                    f"[1:v]setpts=PTS-STARTPTS,fps={self.settings.fps},"
                    f"scale={camera_width}:-2,setsar=1[cam];"
                    f"[screen][cam]overlay=x={x}:y={y}:eof_action=repeat[v]"
                )
                self._run([
                    str(self.ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(video), "-i", str(camera),
                    "-filter_complex", overlay, "-map", "[v]", "-map", "0:a:0",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "25",
                    "-pix_fmt", "yuv420p", "-c:a", "copy",
                    "-movflags", "+faststart", str(with_camera),
                ])
                video = with_camera
            if audio is None:
                padded = self._directory / f"ready-{index:03}.mp4"
                self._run([
                    str(self.ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(video), "-map", "0:v:0", "-map", "0:a:0",
                    "-c:v", "copy", "-af", "apad", "-c:a", "aac", "-b:a", "128k",
                    "-shortest", "-movflags", "+faststart", str(padded),
                ])
                ready.append(padded)
                continue
            mixed = self._directory / f"ready-{index:03}.mp4"
            self._run([
                str(self.ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(video), "-i", str(audio),
                "-filter_complex",
                "[0:a]aresample=async=1:first_pts=0,apad[mic];"
                "[1:a]aresample=async=1:first_pts=0,apad[sys];"
                "[mic][sys]amix=inputs=2:duration=longest:dropout_transition=0[a]",
                "-map", "0:v:0", "-map", "[a]", "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart",
                str(mixed),
            ])
            ready.append(mixed)
        if len(ready) == 1:
            shutil.move(str(ready[0]), str(self.settings.output))
            return
        concat_file = self._directory / "concat.txt"
        concat_file.write_text(
            "".join(f"file '{path.name}'\n" for path in ready),
            encoding="utf-8",
        )
        self._run([
            str(self.ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat_file),
            "-c", "copy", "-movflags", "+faststart", str(self.settings.output),
        ])

    def _run(self, command: list[str]) -> None:
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=3600,
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(decode_ffmpeg_output(result.stderr)[-2500:])
