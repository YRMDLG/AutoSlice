"""识别子进程树的生命周期：启动握手、取消、父进程退出后的后代清理。"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import threading
import time
from ctypes import wintypes

# 挂入 Job 成功前只等待握手，不执行用户模块，也不会启动 FFmpeg 或加载模型。
_BOOTSTRAP = """
import sys
if sys.stdin.buffer.read(1) != b'1':
    raise SystemExit(125)
sys.stdin.close()
import runpy
args = sys.argv[1:]
while args and args[0] in ('-u', '-B'):
    args.pop(0)
if args[0] == '-m':
    sys.argv = args[1:]
    runpy.run_module(args[1], run_name='__main__', alter_sys=True)
elif args[0] == '-c':
    sys.argv = ['-c', *args[2:]]
    exec(compile(args[1], '<string>', 'exec'), {'__name__': '__main__'})
else:
    sys.argv = args
    runpy.run_path(args[0], run_name='__main__')
"""


class _BasicLimit(ctypes.Structure):
    _fields_ = [("user", ctypes.c_int64), ("job", ctypes.c_int64), ("flags", wintypes.DWORD),
                ("min_working", ctypes.c_size_t), ("max_working", ctypes.c_size_t),
                ("active", wintypes.DWORD), ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]


class _ExtendedLimit(ctypes.Structure):
    _fields_ = [("basic", _BasicLimit), ("io", ctypes.c_uint64 * 6),
                ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]


class _Accounting(ctypes.Structure):
    _fields_ = [("times", ctypes.c_int64 * 4), ("page_faults", wintypes.DWORD),
                ("total", wintypes.DWORD), ("active", wintypes.DWORD), ("terminated", wintypes.DWORD)]


class WindowsJob:
    """持有一个不可继承、关闭即终止成员的 Windows Job 句柄。"""

    def __init__(self) -> None:
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([wintypes.LPVOID, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD], wintypes.BOOL),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "QueryInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID], wintypes.BOOL),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = arguments, result
        self._lock = threading.Lock()
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = _ExtendedLimit()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, process: subprocess.Popen) -> None:
        if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def terminate(self) -> None:
        with self._lock:
            if self.handle and not self.api.TerminateJobObject(self.handle, 1):
                raise ctypes.WinError(ctypes.get_last_error())

    def finish(self) -> None:
        """仅在后台等待全部成员退出，随后关闭句柄。"""
        try:
            self.terminate()
            deadline = time.monotonic() + 5
            while True:
                info = _Accounting()
                if not self.api.QueryInformationJobObject(self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
                    raise ctypes.WinError(ctypes.get_last_error())
                if not info.active:
                    return
                if time.monotonic() >= deadline:
                    raise OSError("识别子进程树未能在 5 秒内退出")
                time.sleep(0.01)
        finally:
            self.close()

    def close(self) -> None:
        with self._lock:
            if self.handle:
                handle, self.handle = self.handle, None
                if not self.api.CloseHandle(handle):
                    raise ctypes.WinError(ctypes.get_last_error())


class ProcessTree:
    """由调用方读输出；独立收尾线程保证父进程退出后仍会清理后代。"""

    def __init__(self, program: str, arguments: list[str], environment: dict[str, str]) -> None:
        self.job = WindowsJob() if os.name == "nt" else None
        self.cleanup_error = ""
        self.process = None
        try:
            self.process = subprocess.Popen(
                [program, "-u", "-c", _BOOTSTRAP, *arguments] if self.job else [program, *arguments],
                stdin=subprocess.PIPE if self.job else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env={**os.environ, **environment},
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if self.job else 0,
                start_new_session=not bool(self.job),
            )
            if self.job:
                self.job.assign(self.process)
                self.process.stdin.write(b"1")
                self.process.stdin.flush()
                self.process.stdin.close()
        except OSError:
            if self.process is not None:
                self.process.kill()
                self.process.wait()
                self.process.stdout.close()
                if self.process.stdin:
                    self.process.stdin.close()
            if self.job:
                self.job.close()
            raise
        self._reaper = threading.Thread(target=self._reap, daemon=True)
        self._reaper.start()

    def cancel(self) -> None:
        if self.job:
            self.job.terminate()
        else:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _reap(self) -> None:
        self.process.wait()
        try:
            if self.job:
                self.job.finish()
            else:
                self.cancel()
        except OSError as exc:
            self.cleanup_error = f"识别子进程清理失败：{exc}"

    def wait(self) -> int:
        self._reaper.join()
        return self.process.returncode
