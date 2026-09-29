"""libmpv 的 Qt 适配器；所有 mpv API 调用在专用工作线程串行执行。"""

from __future__ import annotations

import ctypes
import os
import sys
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal


def libmpv_path() -> Path:
    candidates = [
        os.environ.get("AUTOSLICE_LIBMPV_PATH"),
        str(Path(sys.executable).parent / "libmpv-2.dll"),
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "AutoSlice" / "bin" / "libmpv-2.dll"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise FileNotFoundError("未找到 libmpv-2.dll；请在 AUTOSLICE_LIBMPV_PATH 指定开发用 DLL")


class MpvAdapter(QObject):
    """以信号发布播放器状态，Qt 页面不接触 libmpv 指针。"""

    status_changed = Signal(object)
    error = Signal(str)
    _completed = Signal(str, int, object, object)

    def __init__(self, hwnd: int, dll: Path | None = None, parent=None):
        super().__init__(parent)
        self.hwnd = hwnd
        self.dll = dll or libmpv_path()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="autoslice-mpv")
        self._library = None
        self._handle = None
        self._poll_pending = False
        self._closed = False
        self._hardware_reported = False
        self._subtitle_loaded = False
        self._subtitle_path: Path | None = None
        self._generation = 0
        self._command_serial = 0
        self._completed.connect(self._on_completed)
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(150)
        self._poll_timer.timeout.connect(self._poll)

    def _bind(self):
        library = ctypes.CDLL(str(self.dll))
        library.mpv_create.restype = ctypes.c_void_p
        library.mpv_set_option_string.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
        library.mpv_set_option_string.restype = ctypes.c_int
        library.mpv_initialize.argtypes = [ctypes.c_void_p]
        library.mpv_initialize.restype = ctypes.c_int
        library.mpv_command.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_char_p)]
        library.mpv_command.restype = ctypes.c_int
        library.mpv_set_property_string.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
        library.mpv_set_property_string.restype = ctypes.c_int
        library.mpv_get_property.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int,
                                             ctypes.c_void_p]
        library.mpv_get_property.restype = ctypes.c_int
        library.mpv_terminate_destroy.argtypes = [ctypes.c_void_p]
        library.mpv_get_property_string.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        library.mpv_get_property_string.restype = ctypes.c_void_p
        library.mpv_free.argtypes = [ctypes.c_void_p]
        self._library = library

    def _check(self, result: int, action: str):
        if result < 0:
            raise RuntimeError(f"播放器{action}失败，错误码 {result}")

    def _command(self, *parts):
        if self._handle is None:
            raise RuntimeError("播放器尚未就绪")
        encoded = [str(part).encode("utf-8") for part in parts]
        args = (ctypes.c_char_p * (len(encoded) + 1))(*encoded, None)
        self._check(self._library.mpv_command(self._handle, args), str(parts[0]))

    def _set_property(self, name: str, value: str):
        """在播放器线程内设置字符串属性。"""

        if self._handle is None:
            raise RuntimeError("播放器尚未就绪")
        self._check(
            self._library.mpv_set_property_string(
                self._handle, name.encode("utf-8"), value.encode("utf-8")
            ),
            f"设置{name}",
        )

    def _submit(self, kind: str, action):
        if self._closed:
            return
        generation = self._generation
        future: Future = self._pool.submit(action)
        def finish(done):
            try:
                self._completed.emit(kind, generation, done.result(), None)
            except Exception as exc:
                self._completed.emit(kind, generation, None, exc)
        future.add_done_callback(finish)

    def load(self, video: str | Path):
        path = Path(video)
        self._generation += 1
        self._command_serial += 1
        self._poll_pending = False
        self._poll_timer.stop()
        self._submit("load", lambda: self._load(path))

    def _load(self, path: Path):
        if self._library is None:
            self._bind()
        if self._handle is not None:
            self._library.mpv_terminate_destroy(self._handle)
            self._handle = None
        if not path.is_file():
            raise FileNotFoundError("视频文件不存在")
        self._hardware_reported = False
        self._subtitle_loaded = False
        self._subtitle_path = None
        handle = self._library.mpv_create()
        if not handle:
            raise RuntimeError("播放器初始化失败")
        self._handle = handle
        for name, value in (
            (b"wid", str(self.hwnd).encode("ascii")),
            (b"vo", b"gpu"),
            (b"hwdec", b"auto"),
            (b"pause", b"yes"),
            (b"volume", b"50"),
            (b"hr-seek", b"yes"),
        ):
            self._check(self._library.mpv_set_option_string(handle, name, value),
                        name.decode("ascii"))
        self._check(self._library.mpv_initialize(handle), "初始化")
        self._command("loadfile", path)
        return str(path)

    def play(self):
        self._command_serial += 1
        self._submit("command", lambda: self._command("set", "pause", "no"))

    def pause(self):
        self._command_serial += 1
        self._submit("command", lambda: self._command("set", "pause", "yes"))

    def seek(self, seconds: float, *, pause: bool = True):
        self._command_serial += 1
        def action():
            if pause:
                self._command("set", "pause", "yes")
            self._command("seek", f"{max(0.0, seconds):.3f}", "absolute+exact")
        self._submit("command", action)

    def set_volume(self, value: int):
        self._submit("command", lambda: self._command("set", "volume", str(value)))

    def set_speed(self, value: float):
        self._submit("command", lambda: self._command("set", "speed", f"{value:.2f}"))

    def set_subtitle(self, subtitle_path: str | Path):
        """加载或刷新私有 ASS 预览，不写回项目字幕。

        预览服务始终使用同一个 ASS 路径。首次显示时添加一次外部轨道，
        后续只调用 `sub-reload`，避免 `sub-remove all` 在轨道尚未存在时
        返回 ``MPV_ERROR_COMMAND (-4)``，也避免反复添加轨道造成叠加。
        """
        path = Path(subtitle_path)
        def action():
            if not self._subtitle_loaded or self._subtitle_path != path:
                self._command("sub-add", path, "select")
                self._subtitle_loaded = True
                self._subtitle_path = path
            else:
                self._command("sub-reload")
            self._set_property("sub-visibility", "yes")
        self._submit("command", action)

    def clear_subtitle(self):
        self._submit("command", lambda: self._set_property("sub-visibility", "no"))

    def _number(self, name: bytes):
        value = ctypes.c_double()
        code = self._library.mpv_get_property(self._handle, name, 5, ctypes.byref(value))
        return value.value if code == 0 else None

    def _flag(self, name: bytes):
        value = ctypes.c_int()
        code = self._library.mpv_get_property(self._handle, name, 3, ctypes.byref(value))
        return bool(value.value) if code == 0 else None

    def _read_status(self):
        if self._handle is None:
            return {}
        status = {
            "position": self._number(b"time-pos"),
            "duration": self._number(b"duration"),
            "paused": self._flag(b"pause"),
            "idle": self._flag(b"idle-active"),
        }
        if status["duration"] and not self._hardware_reported:
            pointer = self._library.mpv_get_property_string(self._handle, b"hwdec-current")
            if pointer:
                try:
                    status["hwdec"] = ctypes.string_at(pointer).decode("utf-8", errors="replace")
                    self._hardware_reported = True
                finally:
                    self._library.mpv_free(pointer)
        return status

    def _poll(self):
        if self._poll_pending:
            return
        self._poll_pending = True
        serial = self._command_serial
        self._submit("status", lambda: (serial, self._read_status()))

    def _on_completed(self, kind, generation, result, error):
        if self._closed or generation != self._generation:
            return
        if kind == "status":
            self._poll_pending = False
        if error:
            self.error.emit(str(error))
        elif kind == "load":
            self._poll_timer.start()
        elif kind == "status":
            serial, status = result
            if serial == self._command_serial:
                self.status_changed.emit(status)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._poll_timer.stop()
        def destroy():
            if self._handle is not None:
                self._library.mpv_terminate_destroy(self._handle)
                self._handle = None
        self._pool.submit(destroy)
        self._pool.shutdown(wait=False)
