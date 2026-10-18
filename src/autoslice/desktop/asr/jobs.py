"""界面和识别子进程之间的约定：任务说明（JSON 文件）+ 子进程在标准输出里逐行发的事件。

识别放在单独的子进程里跑：模型占的显存随进程结束一定释放、模型或 CUDA 崩了不会带走界面，
也可以用另一个装了 torch 的 Python 环境来跑（设置里的“识别用的 Python”）。
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .settings import AsrSettings

EVENT_PREFIX = "@@asr "


@dataclass(frozen=True)
class JobSpec:
    video: str
    output: str
    storage_root: str
    settings: dict
    hotwords: list[str] = field(default_factory=list)
    overwrite: bool = False

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), ensure_ascii=False), encoding="utf-8")
        return path

    @classmethod
    def read(cls, path: Path) -> JobSpec:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        names = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in payload.items() if key in names})

    def asr_settings(self) -> AsrSettings:
        names = {item.name for item in fields(AsrSettings)}
        return AsrSettings(**{key: value for key, value in self.settings.items() if key in names})


def format_event(kind: str, **payload) -> str:
    return EVENT_PREFIX + json.dumps({"type": kind, **payload}, ensure_ascii=False)


def parse_event(line: str) -> dict | None:
    """不是事件的行（库自己的打印）返回 None。"""

    text = line.strip()
    if not text.startswith(EVENT_PREFIX):
        return None
    try:
        event = json.loads(text[len(EVENT_PREFIX):])
    except ValueError:
        return None
    return event if isinstance(event, dict) and "type" in event else None


def python_command(module: str, arguments: list[str], python: str = "") -> tuple[str, list[str], dict[str, str]]:
    """(程序, 参数, 额外环境变量)。子进程用的 Python 可能不是当前这个，所以把本项目源码目录放进它的 PYTHONPATH。"""

    import autoslice

    source_root = str(Path(autoslice.__file__).resolve().parent.parent)
    program = python.strip() or sys.executable
    return program, ["-u", "-m", module, *arguments], {
        "PYTHONPATH": source_root, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
    }


def worker_command(spec_path: Path, python: str = "") -> tuple[str, list[str], dict[str, str]]:
    return python_command("autoslice.desktop.asr.worker", [str(spec_path)], python)
