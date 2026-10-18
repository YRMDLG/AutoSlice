"""字幕识别设置：存在应用数据目录的 asr.json，设置页和命令行共用。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from autoslice.desktop.foundation import DesktopStorage

DEFAULT_ENGINE = "qwen3-asr"


@dataclass(frozen=True)
class AsrSettings:
    engine: str = DEFAULT_ENGINE
    # Hugging Face / ModelScope 上的名字，或本机模型文件夹
    model: str = "Qwen/Qwen3-ASR-1.7B"
    # 字级时间对齐模型（Qwen3-ASR 要靠它给每个字时间）；留空则字幕时间只能按字数估算
    aligner: str = "Qwen/Qwen3-ForcedAligner-0.6B"
    device: str = "auto"
    # 跑识别的 Python（装了 torch/识别库的那个环境）；留空用当前这个
    python: str = ""
    hotwords: bool = True
    # 留空由模型自动判断语种
    language: str = "Chinese"
    foreground_filter: bool = False


def settings_path(storage: DesktopStorage) -> Path:
    return storage.root / "asr.json"


def load_settings(storage: DesktopStorage) -> AsrSettings:
    try:
        payload = json.loads(settings_path(storage).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return AsrSettings()
    if not isinstance(payload, dict):
        return AsrSettings()
    known = {item.name: item for item in fields(AsrSettings)}
    values = {}
    for name, field in known.items():
        value = payload.get(name)
        if isinstance(value, type(getattr(AsrSettings(), name))):
            values[name] = value.strip() if isinstance(value, str) else value
    return AsrSettings(**values)


def save_settings(storage: DesktopStorage, settings: AsrSettings) -> Path:
    path = settings_path(storage)
    DesktopStorage._write_json(path, asdict(settings))
    return path
