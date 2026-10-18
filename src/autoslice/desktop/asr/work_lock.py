"""按视频缓存键持有系统文件锁；进程退出自动释放，锁文件不随缓存删除。"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .types import AsrError


class WorkLock:
    """稳定锁文件只提供定位信息，互斥由操作系统保证，不依赖 PID 存活推测。"""

    def __init__(self, workdir: Path) -> None:
        self.path = workdir.parent / "locks" / f"{workdir.name}.lock"
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        try:
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close()
            self.stream = None
            raise AsrError("同一个视频正在识别或清理缓存，请等当前任务结束后再试") from exc
        try:
            self.stream.seek(0)
            self.stream.truncate()
            self.stream.write(json.dumps({"pid": os.getpid(), "started": time.time()}).encode("utf-8"))
            self.stream.flush()
        except OSError:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_exc):
        if self.stream is not None:
            # 关闭描述符即释放锁，保留文件，避免删除/重建锁文件造成两份不同的锁。
            self.stream.close()
            self.stream = None
